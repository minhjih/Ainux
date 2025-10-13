"""Networking automation utilities for Ainux."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..config import ensure_config_dir
from ..context import ContextFabric, load_fabric

PROFILES_FILENAME = "network_profiles.json"
FIREWALL_DIRNAME = "nftables"


class NetworkAutomationError(RuntimeError):
    """Raised when network orchestration fails."""


@dataclass
class QoSPolicy:
    """Describes a simple QoS policy using traffic control primitives."""

    interface: str
    rate_limit_mbps: Optional[int] = None
    burst_mbps: Optional[int] = None
    latency_ms: int = 50

    def to_dict(self) -> Dict[str, object]:
        return {
            "interface": self.interface,
            "rate_limit_mbps": self.rate_limit_mbps,
            "burst_mbps": self.burst_mbps,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "QoSPolicy":
        return cls(
            interface=str(payload.get("interface", "")),
            rate_limit_mbps=(
                int(payload.get("rate_limit_mbps")) if payload.get("rate_limit_mbps") is not None else None
            ),
            burst_mbps=(
                int(payload.get("burst_mbps")) if payload.get("burst_mbps") is not None else None
            ),
            latency_ms=int(payload.get("latency_ms", 50)),
        )


@dataclass
class NetworkProfile:
    """Represents an orchestratable network layout."""

    name: str
    description: str = ""
    interfaces: List[str] = field(default_factory=list)
    vlans: List[Dict[str, object]] = field(default_factory=list)
    qos: List[QoSPolicy] = field(default_factory=list)
    firewall_rules: List[str] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "interfaces": self.interfaces,
            "vlans": self.vlans,
            "qos": [policy.to_dict() for policy in self.qos],
            "firewall_rules": self.firewall_rules,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "NetworkProfile":
        qos_payload = payload.get("qos", [])
        qos: List[QoSPolicy] = []
        for item in qos_payload:
            if isinstance(item, dict):
                qos.append(QoSPolicy.from_dict(item))
        return cls(
            name=str(payload.get("name", "")),
            description=str(payload.get("description", "")),
            interfaces=list(payload.get("interfaces", [])),
            vlans=list(payload.get("vlans", [])),
            qos=qos,
            firewall_rules=list(payload.get("firewall_rules", [])),
            metadata=dict(payload.get("metadata", {})),
        )


def default_profiles_path() -> Path:
    config_path = ensure_config_dir()
    profiles_path = config_path.parent / PROFILES_FILENAME
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    return profiles_path


def _firewall_dir() -> Path:
    config_path = ensure_config_dir()
    path = config_path.parent / FIREWALL_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


class NetworkAutomationService:
    """Apply declarative network profiles and QoS policies."""

    def __init__(
        self,
        *,
        profiles_path: Optional[Path] = None,
        context_fabric: Optional[ContextFabric] = None,
        fabric_path: Optional[Path] = None,
    ) -> None:
        self.profiles_path = Path(profiles_path or default_profiles_path()).expanduser()
        self.fabric = context_fabric
        self.fabric_path = fabric_path
        self.firewall_dir = _firewall_dir()
        self._profiles: Dict[str, NetworkProfile] = {}
        self._load_profiles()

    # ------------------------------------------------------------------
    # Profile management
    # ------------------------------------------------------------------
    def list_profiles(self) -> List[str]:
        return sorted(self._profiles)

    def get_profile(self, name: str) -> NetworkProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise NetworkAutomationError(f"Profile '{name}' not found") from exc

    def save_profile(self, profile: NetworkProfile, *, persist: bool = True) -> None:
        if not profile.name:
            raise NetworkAutomationError("Profile must have a name")
        self._profiles[profile.name] = profile
        if persist:
            self._persist_profiles()
        self._record_event(
            "network.profile.saved",
            {
                "name": profile.name,
                "interfaces": profile.interfaces,
                "qos_count": len(profile.qos),
            },
        )

    def delete_profile(self, name: str) -> bool:
        if name not in self._profiles:
            return False
        del self._profiles[name]
        self._persist_profiles()
        self._record_event("network.profile.deleted", {"name": name})
        firewall_path = self.firewall_dir / f"{name}.nft"
        if firewall_path.exists():
            firewall_path.unlink()
        return True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def apply_profile(self, name: str, *, dry_run: bool = True) -> List[str]:
        profile = self.get_profile(name)
        commands = self._build_commands(profile, persist_files=not dry_run)
        if dry_run:
            return [" ".join(cmd) for cmd in commands]
        for command in commands:
            self._run_command(command)
        self._record_event(
            "network.profile.applied",
            {
                "name": profile.name,
                "interfaces": profile.interfaces,
                "qos": [policy.to_dict() for policy in profile.qos],
            },
        )
        return [" ".join(cmd) for cmd in commands]

    def apply_qos(self, policy: QoSPolicy, *, dry_run: bool = True) -> List[str]:
        commands = self._build_qos_commands(policy)
        if dry_run:
            return [" ".join(cmd) for cmd in commands]
        for command in commands:
            self._run_command(command)
        self._record_event("network.qos.applied", policy.to_dict())
        return [" ".join(cmd) for cmd in commands]

    def snapshot_interfaces(self) -> str:
        ip_path = shutil.which("ip")
        if ip_path is None:
            raise NetworkAutomationError("ip command not available")
        proc = subprocess.run([ip_path, "-o", "addr", "show"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise NetworkAutomationError(f"ip addr failed: {proc.stderr.strip()}")
        self._record_event("network.snapshot.interfaces", {})
        return proc.stdout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_commands(self, profile: NetworkProfile, *, persist_files: bool) -> List[List[str]]:
        commands: List[List[str]] = []
        for vlan in profile.vlans:
            parent = str(vlan.get("parent")) if vlan.get("parent") else None
            vlan_id = vlan.get("id")
            if not parent or vlan_id is None:
                continue
            vlan_iface = f"{parent}.{vlan_id}"
            commands.append(["sudo", "ip", "link", "add", "link", parent, "name", vlan_iface, "type", "vlan", "id", str(vlan_id)])
            commands.append(["sudo", "ip", "link", "set", vlan_iface, "up"])
            address = vlan.get("address")
            if address:
                commands.append(["sudo", "ip", "addr", "add", str(address), "dev", vlan_iface])
        for policy in profile.qos:
            commands.extend(self._build_qos_commands(policy))
        if profile.firewall_rules:
            firewall_path = self.firewall_dir / f"{profile.name}.nft"
            if persist_files:
                payload = "\n".join(profile.firewall_rules) + "\n"
                firewall_path.write_text(payload, encoding="utf-8")
            commands.append(["sudo", "nft", "-f", str(firewall_path)])
        return commands

    def _build_qos_commands(self, policy: QoSPolicy) -> List[List[str]]:
        if not policy.interface:
            raise NetworkAutomationError("QoS policy requires an interface")
        commands: List[List[str]] = []
        commands.append(["sudo", "tc", "qdisc", "replace", "dev", policy.interface, "root", "handle", "1:", "htb", "default", "30"])
        if policy.rate_limit_mbps:
            burst = policy.burst_mbps or policy.rate_limit_mbps
            commands.append(
                [
                    "sudo",
                    "tc",
                    "class",
                    "replace",
                    "dev",
                    policy.interface,
                    "parent",
                    "1:",
                    "classid",
                    "1:1",
                    "htb",
                    "rate",
                    f"{policy.rate_limit_mbps}mbit",
                    "ceil",
                    f"{policy.rate_limit_mbps}mbit",
                ]
            )
            commands.append(
                [
                    "sudo",
                    "tc",
                    "qdisc",
                    "replace",
                    "dev",
                    policy.interface,
                    "parent",
                    "1:1",
                    "handle",
                    "10:",
                    "tbf",
                    "rate",
                    f"{policy.rate_limit_mbps}mbit",
                    "burst",
                    f"{burst}mbit",
                    "latency",
                    f"{policy.latency_ms}ms",
                ]
            )
        return commands

    def _run_command(self, command: Sequence[str]) -> None:
        proc = subprocess.run(list(command), capture_output=True, text=True)
        if proc.returncode != 0:
            raise NetworkAutomationError(
                f"Command '{' '.join(command)}' failed: {proc.stderr.strip()}"
            )

    def _load_profiles(self) -> None:
        if not self.profiles_path.exists():
            self._profiles = {}
            return
        try:
            payload = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise NetworkAutomationError(f"Failed to load network profiles: {exc}") from exc
        profiles: Dict[str, NetworkProfile] = {}
        for item in payload.get("profiles", []):
            if not isinstance(item, dict):
                continue
            profile = NetworkProfile.from_dict(item)
            if profile.name:
                profiles[profile.name] = profile
        self._profiles = profiles

    def _persist_profiles(self) -> None:
        payload = {"profiles": [profile.to_dict() for profile in self._profiles.values()]}
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        tmp_path = self.profiles_path.with_suffix(".tmp")
        tmp_path.write_text(serialized, encoding="utf-8")
        tmp_path.replace(self.profiles_path)

    def _record_event(self, event_type: str, payload: Dict[str, object]) -> None:
        fabric = self._ensure_fabric()
        if fabric is None:
            return
        fabric.record_event(event_type, payload)
        if self.fabric_path:
            fabric.save(self.fabric_path)

    def _ensure_fabric(self) -> Optional[ContextFabric]:
        if self.fabric is None and self.fabric_path:
            self.fabric = load_fabric(self.fabric_path)
        return self.fabric

