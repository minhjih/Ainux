"""Ainux AI client package."""

from .context import ContextFabric, ContextSnapshot
from .hardware import HardwareAutomationService, HardwareCatalog
from .orchestration import AinuxOrchestrator, OrchestrationError

__all__ = [
    "__version__",
    "AinuxOrchestrator",
    "ContextFabric",
    "ContextSnapshot",
    "HardwareAutomationService",
    "HardwareCatalog",
    "OrchestrationError",
]
__version__ = "0.5.0"
