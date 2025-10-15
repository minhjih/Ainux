"""Driver and firmware catalog management."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

CATALOG_ENV = "AINUX_HARDWARE_CATALOG"


@dataclass
class HardwareComponent:
    """Detected or catalogued hardware component."""

    identifier: str
    name: str
    category: str
    vendor: Optional[str] = None
    model: Optional[str] = None
    bus: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    driver: Optional[str] = None
    firmware: Optional[str] = None
    capabilities: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DriverPackage:
    """Driver package information."""

    name: str
    version: str
    packages: List[str]
    kernel_modules: List[str] = field(default_factory=list)
    description: Optional[str] = None
    vendor: Optional[str] = None
    supported_ids: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FirmwarePackage:
    """Firmware blob or bundle information."""

    name: str
    version: str
    files: List[str]
    description: Optional[str] = None
    vendor: Optional[str] = None
    supported_ids: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HardwareCatalog:
    """Persistent catalog of hardware components, drivers, and firmware."""

    components: Dict[str, HardwareComponent] = field(default_factory=dict)
    drivers: Dict[str, DriverPackage] = field(default_factory=dict)
    firmware: Dict[str, FirmwarePackage] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "components": {key: asdict(value) for key, value in self.components.items()},
            "drivers": {key: asdict(value) for key, value in self.drivers.items()},
            "firmware": {key: asdict(value) for key, value in self.firmware.items()},
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "HardwareCatalog":
        components = {
            key: HardwareComponent(**value)
            for key, value in payload.get("components", {}).items()
        }
        drivers = {
            key: DriverPackage(**value) for key, value in payload.get("drivers", {}).items()
        }
        firmware = {
            key: FirmwarePackage(**value)
            for key, value in payload.get("firmware", {}).items()
        }
        metadata = dict(payload.get("metadata", {}))
        return cls(components=components, drivers=drivers, firmware=firmware, metadata=metadata)

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or default_catalog_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "HardwareCatalog":
        path = path or default_catalog_path()
        if not path.exists():
            catalog = cls()
            catalog.ensure_defaults()
            return catalog
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid hardware catalog at {path}: {exc}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Hardware catalog at {path} must be a JSON object")
        catalog = cls.from_dict(payload)
        catalog.ensure_defaults()
        return catalog

    def upsert_component(self, component: HardwareComponent) -> None:
        self.components[component.identifier] = component

    def upsert_driver(self, driver: DriverPackage) -> None:
        self.drivers[driver.name] = driver

    def upsert_firmware(self, firmware: FirmwarePackage) -> None:
        self.firmware[firmware.name] = firmware

    def components_for_tag(self, tag: str) -> List[HardwareComponent]:
        return [component for component in self.components.values() if tag in component.tags]

    def match_drivers(self, component: HardwareComponent) -> List[DriverPackage]:
        matches: List[DriverPackage] = []
        for driver in self.drivers.values():
            if component.identifier in driver.supported_ids:
                matches.append(driver)
                continue
            if component.vendor and component.vendor in driver.supported_ids:
                matches.append(driver)
        return matches

    def match_firmware(self, component: HardwareComponent) -> List[FirmwarePackage]:
        matches: List[FirmwarePackage] = []
        for firmware in self.firmware.values():
            if component.identifier in firmware.supported_ids:
                matches.append(firmware)
                continue
            if component.vendor and component.vendor in firmware.supported_ids:
                matches.append(firmware)
        return matches

    def ensure_defaults(self) -> None:
        """Seed catalog metadata with default capability blueprints."""

        defaults = self.metadata.setdefault("blueprints", {})
        defaults.setdefault(
            "gpu.cuda",
            {
                "description": "CUDA 및 NVIDIA 드라이버 자동 구성",
                "packages": ["nvidia-driver-535", "nvidia-cuda-toolkit"],
                "post_steps": ["nvidia-smi"],
            },
        )
        defaults.setdefault(
            "network.dpdk",
            {
                "description": "DPDK 및 고성능 네트워크 패스 구성",
                "packages": ["dpdk", "hugepages"],
            },
        )
        defaults.setdefault(
            "storage.raid",
            {
                "description": "mdadm 기반 소프트웨어 RAID 구성",
                "packages": ["mdadm"],
            },
        )

    def list_blueprints(self) -> Dict[str, Any]:
        self.ensure_defaults()
        return dict(self.metadata.get("blueprints", {}))


def default_catalog_path() -> Path:
    override = os.environ.get(CATALOG_ENV)
    if override:
        return Path(override).expanduser()

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        base = Path(config_home).expanduser()
    else:
        base = Path.home() / ".config"
    return base / "ainux" / "hardware_catalog.json"


def merge_components(catalog: HardwareCatalog, components: Iterable[HardwareComponent]) -> None:
    for component in components:
        catalog.upsert_component(component)
