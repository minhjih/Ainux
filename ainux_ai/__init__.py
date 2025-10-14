"""Ainux AI client package."""

from .context import ContextFabric, ContextSnapshot
from .hardware import HardwareAutomationService, HardwareCatalog
from .infrastructure import (
    SchedulerService,
    NetworkAutomationService,
    ClusterHealthService,
)
from .orchestration import AinuxOrchestrator, OrchestrationError

__all__ = [
    "__version__",
    "AinuxOrchestrator",
    "ContextFabric",
    "ContextSnapshot",
    "HardwareAutomationService",
    "HardwareCatalog",
    "SchedulerService",
    "NetworkAutomationService",
    "ClusterHealthService",
    "OrchestrationError",
]
__version__ = "0.8.0"
