"""Cluster health aggregation for infrastructure automation."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from ..context import ContextFabric, load_fabric


class ClusterHealthError(RuntimeError):
    """Raised when health telemetry cannot be collected."""


@dataclass
class HealthReport:
    """Structured snapshot describing the state of the cluster."""

    timestamp: datetime
    load_average: Sequence[float]
    cpu_count: int
    memory: Dict[str, float]
    disk: Dict[str, float]
    gpus: List[Dict[str, object]] = field(default_factory=list)
    scheduler_queue: List[Dict[str, object]] = field(default_factory=list)
    network_interfaces: List[Dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "load_average": list(self.load_average),
            "cpu_count": self.cpu_count,
            "memory": self.memory,
            "disk": self.disk,
            "gpus": self.gpus,
            "scheduler_queue": self.scheduler_queue,
            "network_interfaces": self.network_interfaces,
        }


class ClusterHealthService:
    """Collects system metrics and normalizes them for orchestrators."""

    def __init__(
        self,
        *,
        context_fabric: Optional[ContextFabric] = None,
        fabric_path: Optional[Path] = None,
    ) -> None:
        self.fabric = context_fabric
        self.fabric_path = fabric_path

    def snapshot(self) -> HealthReport:
        timestamp = datetime.now(timezone.utc)
        load_average = self._load_average()
        memory = self._memory()
        disk = self._disk()
        gpus = self._gpus()
        scheduler_queue = self._scheduler_queue()
        network = self._network_interfaces()
        report = HealthReport(
            timestamp=timestamp,
            load_average=load_average,
            cpu_count=os.cpu_count() or 0,
            memory=memory,
            disk=disk,
            gpus=gpus,
            scheduler_queue=scheduler_queue,
            network_interfaces=network,
        )
        self._record_event("cluster.health.snapshot", report.to_dict())
        return report

    def watch(self, *, interval: float = 10.0, limit: Optional[int] = None) -> Iterator[HealthReport]:
        count = 0
        while limit is None or count < limit:
            yield self.snapshot()
            count += 1
            if limit is not None and count >= limit:
                break
            time.sleep(max(interval, 0.1))

    # ------------------------------------------------------------------
    # Collectors
    # ------------------------------------------------------------------
    def _load_average(self) -> Sequence[float]:
        try:
            return os.getloadavg()
        except OSError:
            return (0.0, 0.0, 0.0)

    def _memory(self) -> Dict[str, float]:
        meminfo_path = Path("/proc/meminfo")
        memory: Dict[str, float] = {"total_mb": 0.0, "available_mb": 0.0}
        if meminfo_path.exists():
            for line in meminfo_path.read_text(encoding="utf-8").splitlines():
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                value = parts[1].strip().split()[0]
                try:
                    kb = float(value)
                except ValueError:
                    continue
                if key == "MemTotal":
                    memory["total_mb"] = kb / 1024
                elif key == "MemAvailable":
                    memory["available_mb"] = kb / 1024
        return memory

    def _disk(self) -> Dict[str, float]:
        usage = shutil.disk_usage("/")
        return {
            "path": "/",
            "total_gb": round(usage.total / (1024 ** 3), 2),
            "used_gb": round((usage.total - usage.free) / (1024 ** 3), 2),
            "free_gb": round(usage.free / (1024 ** 3), 2),
        }

    def _gpus(self) -> List[Dict[str, object]]:
        nvidia = shutil.which("nvidia-smi")
        if nvidia is None:
            return []
        query = [
            nvidia,
            "--query-gpu=index,name,driver_version,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
        proc = subprocess.run(query, capture_output=True, text=True)
        if proc.returncode != 0:
            return []
        gpus: List[Dict[str, object]] = []
        for line in proc.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 6:
                continue
            gpus.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "driver": parts[2],
                    "memory_total_mb": float(parts[3]),
                    "memory_used_mb": float(parts[4]),
                    "utilisation_percent": float(parts[5]),
                }
            )
        return gpus

    def _scheduler_queue(self) -> List[Dict[str, object]]:
        squeue = shutil.which("squeue")
        if squeue is None:
            return []
        args = [squeue, "--noheader", "--format=%i|%j|%P|%T|%M"]
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            return []
        queue: List[Dict[str, object]] = []
        for line in proc.stdout.splitlines():
            job_id, name, partition, state, elapsed = (line.split("|") + [""] * 5)[:5]
            queue.append(
                {
                    "job_id": job_id.strip(),
                    "name": name.strip(),
                    "partition": partition.strip(),
                    "state": state.strip(),
                    "elapsed": elapsed.strip(),
                }
            )
        return queue

    def _network_interfaces(self) -> List[Dict[str, object]]:
        dev_path = Path("/proc/net/dev")
        if not dev_path.exists():
            return []
        interfaces: List[Dict[str, object]] = []
        lines = dev_path.read_text(encoding="utf-8").splitlines()[2:]
        for line in lines:
            if ":" not in line:
                continue
            name_part, stats_part = line.split(":", 1)
            name = name_part.strip()
            stats = stats_part.split()
            if len(stats) < 16:
                continue
            interfaces.append(
                {
                    "name": name,
                    "rx_bytes": int(stats[0]),
                    "rx_packets": int(stats[1]),
                    "tx_bytes": int(stats[8]),
                    "tx_packets": int(stats[9]),
                }
            )
        return interfaces

    # ------------------------------------------------------------------
    # Fabric helpers
    # ------------------------------------------------------------------
    def _ensure_fabric(self) -> Optional[ContextFabric]:
        if self.fabric is None and self.fabric_path:
            self.fabric = load_fabric(self.fabric_path)
        return self.fabric

    def _record_event(self, event_type: str, payload: Dict[str, object]) -> None:
        fabric = self._ensure_fabric()
        if fabric is None:
            return
        fabric.record_event(event_type, payload)
        if self.fabric_path:
            fabric.save(self.fabric_path)

