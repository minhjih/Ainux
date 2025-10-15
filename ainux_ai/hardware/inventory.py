"""System inventory helpers for detecting hardware components."""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path
from typing import Iterable, List

from .catalog import HardwareComponent

PCI_RE = re.compile(r"^(?P<slot>[0-9a-fA-F:.]+)\s+(?P<class>[^:]+):\s+(?P<vendor>[^[]+)(\[(?P<id>[^\]]+)\])?")
USB_RE = re.compile(r"ID\s+(?P<id>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(?P<vendor>[^\s]+)\s*(?P<name>.*)")


def _run_command(command: List[str]) -> str:
    try:
        output = subprocess.check_output(command, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return ""
    return output.decode("utf-8", errors="ignore")


def _parse_pci(output: str) -> Iterable[HardwareComponent]:
    for line in output.splitlines():
        match = PCI_RE.match(line.strip())
        if not match:
            continue
        vendor = match.group("vendor").strip()
        identifier = match.group("id") or match.group("slot")
        yield HardwareComponent(
            identifier=identifier,
            name=f"PCI {match.group('class').strip()} - {vendor}",
            category="pci",
            vendor=vendor,
            bus="pci",
            metadata={"slot": match.group("slot")},
        )


def _parse_usb(output: str) -> Iterable[HardwareComponent]:
    for line in output.splitlines():
        match = USB_RE.search(line)
        if not match:
            continue
        vendor = match.group("vendor").strip()
        identifier = match.group("id")
        name = match.group("name").strip() or "USB Device"
        yield HardwareComponent(
            identifier=identifier,
            name=f"USB {vendor} {name}",
            category="usb",
            vendor=vendor,
            bus="usb",
        )


def _parse_block_devices() -> Iterable[HardwareComponent]:
    sys_block = Path("/sys/block")
    if not sys_block.exists():
        return []
    for entry in sys_block.iterdir():
        model_path = entry / "device/model"
        vendor_path = entry / "device/vendor"
        try:
            model = model_path.read_text(encoding="utf-8").strip()
        except OSError:
            model = ""
        try:
            vendor = vendor_path.read_text(encoding="utf-8").strip()
        except OSError:
            vendor = ""
        identifier = entry.name
        yield HardwareComponent(
            identifier=identifier,
            name=f"Block Device {identifier}",
            category="storage",
            vendor=vendor or None,
            model=model or None,
            bus="block",
        )


def _gather_dmi() -> Iterable[HardwareComponent]:
    dmi_path = Path("/sys/devices/virtual/dmi/id")
    if not dmi_path.exists():
        return []
    system_vendor = (dmi_path / "sys_vendor").read_text(encoding="utf-8", errors="ignore").strip()
    product_name = (dmi_path / "product_name").read_text(encoding="utf-8", errors="ignore").strip()
    product_version = (dmi_path / "product_version").read_text(encoding="utf-8", errors="ignore").strip()
    board_name = (dmi_path / "board_name").read_text(encoding="utf-8", errors="ignore").strip()

    yield HardwareComponent(
        identifier="system",
        name=f"{system_vendor} {product_name}",
        category="system",
        vendor=system_vendor or None,
        model=product_name or None,
        metadata={"version": product_version, "board": board_name},
    )


def _detect_nvidia_gpu() -> Iterable[HardwareComponent]:
    smi_output = _run_command(["nvidia-smi", "--query-gpu=name,uuid,driver_version", "--format=csv,noheader"])
    if not smi_output:
        return []
    for line in smi_output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue
        name, uuid, driver_version = parts[:3]
        yield HardwareComponent(
            identifier=uuid,
            name=name,
            category="gpu",
            vendor="nvidia",
            model=name,
            tags=["gpu", "cuda"],
            metadata={"driver_version": driver_version},
        )


def scan_system_inventory() -> List[HardwareComponent]:
    """Collect hardware components from multiple sources."""

    components: List[HardwareComponent] = []

    pci_output = _run_command(["/usr/bin/env", "lspci", "-nn"])
    if pci_output:
        components.extend(_parse_pci(pci_output))

    usb_output = _run_command(["/usr/bin/env", "lsusb"])
    if usb_output:
        components.extend(_parse_usb(usb_output))

    components.extend(_parse_block_devices())
    components.extend(_gather_dmi())
    components.extend(_detect_nvidia_gpu())

    uname = platform.uname()
    components.append(
        HardwareComponent(
            identifier="kernel",
            name=f"Linux {uname.release}",
            category="kernel",
            vendor=uname.system,
            model=uname.machine,
            metadata={"version": uname.version},
        )
    )

    # Include virtualization hints when running inside a container/VM
    if Path("/proc/1/cgroup").exists():
        control_groups = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
        if "docker" in control_groups or "kubepods" in control_groups:
            components.append(
                HardwareComponent(
                    identifier="environment.container",
                    name="Container Environment",
                    category="environment",
                    tags=["containerized"],
                    metadata={"details": control_groups.strip()},
                )
            )

    return components
