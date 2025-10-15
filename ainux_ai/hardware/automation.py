"""Automation routines that combine inventory, catalog, and telemetry."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from ..context import ContextFabric
from .catalog import (
    HardwareCatalog,
    HardwareComponent,
    DriverPackage,
    FirmwarePackage,
    default_catalog_path,
    merge_components,
)
from .dependencies import DependencyGraph
from .inventory import scan_system_inventory
from .telemetry import TelemetryCollector, TelemetrySample


class HardwareAutomationError(RuntimeError):
    """Raised when automation workflows fail."""


@dataclass
class AutomationPlan:
    components: List[HardwareComponent]
    drivers: List[DriverPackage]
    firmware: List[FirmwarePackage]
    install_plan: List[Dict[str, object]]


class HardwareAutomationService:
    """Coordinates hardware discovery, cataloging, and execution plans."""

    def __init__(
        self,
        *,
        catalog_path: Optional[Path] = None,
        context_fabric: Optional[ContextFabric] = None,
        fabric_path: Optional[Path] = None,
    ) -> None:
        self.catalog_path = catalog_path or default_catalog_path()
        self.catalog = HardwareCatalog.load(self.catalog_path)
        self.fabric = context_fabric
        self.fabric_path = fabric_path
        self.telemetry = TelemetryCollector()

    def refresh_inventory(self, *, persist: bool = True) -> List[HardwareComponent]:
        components = scan_system_inventory()
        if persist:
            merge_components(self.catalog, components)
            self.catalog.save(self.catalog_path)
            self._record_event(
                "hardware.inventory.refresh",
                {
                    "component_count": len(components),
                },
            )
        return components

    def recommend(self, components: Optional[Iterable[HardwareComponent]] = None) -> AutomationPlan:
        if components is None:
            components = self.catalog.components.values()
        selected_components = list(components)

        matched_drivers: Dict[str, DriverPackage] = {}
        matched_firmware: Dict[str, FirmwarePackage] = {}

        for component in selected_components:
            for driver in self.catalog.match_drivers(component):
                matched_drivers[driver.name] = driver
            for firmware in self.catalog.match_firmware(component):
                matched_firmware[firmware.name] = firmware

        graph = self._build_dependency_graph(matched_drivers.values(), matched_firmware.values())
        install_plan = graph.to_install_plan()
        return AutomationPlan(
            components=selected_components,
            drivers=list(matched_drivers.values()),
            firmware=list(matched_firmware.values()),
            install_plan=install_plan,
        )

    def add_driver_blueprint(
        self,
        driver: DriverPackage,
        *,
        persist: bool = True,
    ) -> None:
        self.catalog.upsert_driver(driver)
        if persist:
            self.catalog.save(self.catalog_path)
        self._record_event(
            "hardware.driver.cataloged",
            {
                "driver": driver.name,
                "version": driver.version,
                "packages": driver.packages,
            },
        )

    def add_firmware_blueprint(self, firmware: FirmwarePackage, *, persist: bool = True) -> None:
        self.catalog.upsert_firmware(firmware)
        if persist:
            self.catalog.save(self.catalog_path)
        self._record_event(
            "hardware.firmware.cataloged",
            {
                "firmware": firmware.name,
                "version": firmware.version,
            },
        )

    def capture_telemetry(self) -> TelemetrySample:
        sample = self.telemetry.collect()
        self._record_event(
            "hardware.telemetry.sample",
            {
                "cpu_util": sample.cpu_utilisation,
                "mem_used": sample.memory_used_mb,
                "mem_total": sample.memory_total_mb,
                "disk_free": sample.disk_free_gb,
                "disk_total": sample.disk_total_gb,
            },
        )
        return sample

    def execute_plan(self, plan: Sequence[Dict[str, object]], *, dry_run: bool = True) -> List[str]:
        commands: List[str] = []
        for item in plan:
            name = item.get("name")
            kind = item.get("kind")
            metadata = item.get("metadata", {})
            if kind == "apt_package":
                pkgs = metadata.get("packages") or [name]
                command = ["sudo", "apt-get", "install", "-y", *pkgs]
            elif kind == "modprobe":
                command = ["sudo", "modprobe", name]
            elif kind == "firmware" and metadata.get("files"):
                command = ["sudo", "cp", "-t", "/lib/firmware", *metadata["files"]]
            else:
                command = metadata.get("command")
                if not command:
                    continue
                if isinstance(command, str):
                    command = [command]
            commands.append(" ".join(map(str, command)))
            if not dry_run:
                try:
                    subprocess.check_call(command)
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise HardwareAutomationError(
                        f"Failed to execute step '{name}': {exc}"
                    ) from exc
        if commands:
            self._record_event(
                "hardware.automation.plan.executed",
                {
                    "dry_run": dry_run,
                    "steps": commands,
                },
            )
        return commands

    def _build_dependency_graph(
        self,
        drivers: Iterable[DriverPackage],
        firmware: Iterable[FirmwarePackage],
    ) -> DependencyGraph:
        graph = DependencyGraph()
        for driver in drivers:
            graph.add_node(driver.name, "apt_package", packages=driver.packages, version=driver.version)
            for module in driver.kernel_modules:
                mod_name = f"modprobe:{module}"
                graph.add_node(mod_name, "modprobe", module=module)
                graph.add_dependency(mod_name, driver.name)
            for requirement in driver.requires:
                graph.add_dependency(driver.name, requirement)
            for provided in driver.provides:
                graph.add_node(provided, "virtual", provider=driver.name)
                graph.add_dependency(driver.name, provided)

        for item in firmware:
            graph.add_node(item.name, "firmware", files=item.files, version=item.version)
            for requirement in item.requires:
                graph.add_dependency(item.name, requirement)

        # Always ensure kernel headers are installed before NVIDIA modules
        if any(driver for driver in drivers if "nvidia" in (driver.vendor or "")):
            graph.add_node("linux-headers-generic", "apt_package", packages=["linux-headers-generic"])
            for driver in drivers:
                if "nvidia" in (driver.vendor or ""):
                    graph.add_dependency(driver.name, "linux-headers-generic")

        return graph

    def _record_event(self, event_type: str, payload: Dict[str, object]) -> None:
        if not self.fabric:
            return
        self.fabric.record_event(event_type, payload)
        self.fabric.save(self.fabric_path)
