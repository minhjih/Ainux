"""Telemetry collection utilities for hardware automation."""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class TelemetrySample:
    timestamp: float
    cpu_utilisation: float
    memory_used_mb: float
    memory_total_mb: float
    gpu_utilisation: Optional[float] = None
    gpu_memory_used_mb: Optional[float] = None
    gpu_memory_total_mb: Optional[float] = None
    disk_free_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    metadata: Dict[str, object] = None


class TelemetryCollector:
    """Collects lightweight telemetry snapshots from the local system."""

    def __init__(self, *, sample_disk: bool = True) -> None:
        self.sample_disk = sample_disk

    def collect(self) -> TelemetrySample:
        cpu_util = _read_cpu_utilisation()
        mem_used, mem_total = _read_memory()
        disk_free = disk_total = None
        if self.sample_disk:
            disk_free, disk_total = _read_disk()

        gpu = _read_nvidia_gpu()
        metadata: Dict[str, object] = {}
        if gpu:
            metadata["gpu"] = gpu

        return TelemetrySample(
            timestamp=time.time(),
            cpu_utilisation=cpu_util,
            memory_used_mb=mem_used,
            memory_total_mb=mem_total,
            gpu_utilisation=gpu.get("utilisation") if gpu else None,
            gpu_memory_used_mb=gpu.get("memory_used") if gpu else None,
            gpu_memory_total_mb=gpu.get("memory_total") if gpu else None,
            disk_free_gb=disk_free,
            disk_total_gb=disk_total,
            metadata=metadata,
        )

    def collect_series(self, samples: int, interval: float = 1.0) -> List[TelemetrySample]:
        result: List[TelemetrySample] = []
        for _ in range(samples):
            result.append(self.collect())
            if interval > 0:
                time.sleep(interval)
        return result


def _read_cpu_utilisation() -> float:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            line = handle.readline()
    except OSError:
        return 0.0
    parts = line.strip().split()
    if len(parts) < 5:
        return 0.0
    values = list(map(float, parts[1:]))
    idle = values[3]
    total = sum(values)
    if total == 0:
        return 0.0
    busy = total - idle
    return round((busy / total) * 100, 2)


def _read_memory() -> (float, float):
    try:
        contents = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return 0.0, 0.0
    meminfo: Dict[str, float] = {}
    for line in contents.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parts = value.strip().split()
        if not parts:
            continue
        try:
            meminfo[key] = float(parts[0])
        except ValueError:
            continue
    total_kb = meminfo.get("MemTotal", 0.0)
    free_kb = meminfo.get("MemFree", 0.0) + meminfo.get("Buffers", 0.0) + meminfo.get("Cached", 0.0)
    used_kb = max(total_kb - free_kb, 0.0)
    return round(used_kb / 1024.0, 2), round(total_kb / 1024.0, 2)


def _read_disk() -> (float, float):
    stat = shutil.disk_usage("/")
    total_gb = stat.total / (1024 ** 3)
    free_gb = stat.free / (1024 ** 3)
    return round(free_gb, 2), round(total_gb, 2)


def _read_nvidia_gpu() -> Optional[Dict[str, float]]:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    line = output.decode("utf-8", errors="ignore").splitlines()
    if not line:
        return None
    parts = [part.strip() for part in line[0].split(",")]
    if len(parts) != 3:
        return None
    try:
        utilisation = float(parts[0])
        mem_used = float(parts[1])
        mem_total = float(parts[2])
    except ValueError:
        return None
    return {
        "utilisation": utilisation,
        "memory_used": mem_used,
        "memory_total": mem_total,
    }
