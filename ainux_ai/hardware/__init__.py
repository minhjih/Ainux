"""Hardware automation toolkit for Ainux."""

from .automation import HardwareAutomationService, HardwareAutomationError
from .catalog import (
    HardwareCatalog,
    HardwareComponent,
    DriverPackage,
    FirmwarePackage,
    default_catalog_path,
)
from .dependencies import DependencyGraph, DependencyCycleError
from .inventory import scan_system_inventory
from .telemetry import TelemetryCollector, TelemetrySample

__all__ = [
    "HardwareAutomationError",
    "HardwareAutomationService",
    "HardwareCatalog",
    "HardwareComponent",
    "DriverPackage",
    "FirmwarePackage",
    "DependencyGraph",
    "DependencyCycleError",
    "TelemetryCollector",
    "TelemetrySample",
    "default_catalog_path",
    "scan_system_inventory",
]
