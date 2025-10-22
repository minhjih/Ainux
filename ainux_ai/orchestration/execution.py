"""Execution runtime for orchestration plans."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Protocol

try:  # pragma: no cover - optional runtime dependency
    import pyautogui  # type: ignore
except Exception:  # pragma: no cover - defensive fallback
    pyautogui = None

from .models import ExecutionResult, PlanStep


class Capability(Protocol):
    """Protocol implemented by concrete execution capabilities."""

    name: str

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        ...


@dataclass
class CapabilityRegistry:
    """Simple registry that maps action names to capabilities."""

    capabilities: MutableMapping[str, Capability] = field(default_factory=dict)

    def register(self, capability: Capability) -> None:
        self.capabilities[capability.name] = capability

    def get(self, action: str) -> Optional[Capability]:
        return self.capabilities.get(action)

    def resolve(self, action: str) -> Capability:
        capability = self.get(action)
        if capability is None:
            raise KeyError(f"No capability registered for action '{action}'")
        return capability


@dataclass
class ActionExecutor:
    """Execute plan steps using registered capabilities."""

    registry: CapabilityRegistry

    def execute_plan(
        self, steps: Iterable[PlanStep], context: Optional[Dict[str, object]] = None
    ) -> List[ExecutionResult]:
        results: List[ExecutionResult] = []
        for step in steps:
            try:
                capability = self.registry.resolve(step.action)
            except KeyError as exc:
                results.append(
                    ExecutionResult(
                        step_id=step.id,
                        status="blocked",
                        error=str(exc),
                    )
                )
                continue
            try:
                result = capability.execute(step, context)
            except Exception as exc:  # pragma: no cover - defensive safety
                results.append(
                    ExecutionResult(
                        step_id=step.id,
                        status="error",
                        error=str(exc),
                    )
                )
            else:
                results.append(result)
        return results


@dataclass
class DryRunCapability:
    """Capability that records the intended action without side effects."""

    name: str

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        description = step.description or f"Execute {step.action}"
        payload = {
            "description": description,
            "parameters": step.parameters,
        }
        return ExecutionResult(
            step_id=step.id,
            status="dry_run",
            output=json.dumps(payload, ensure_ascii=False),
        )


@dataclass
class ShellCommandCapability:
    """Capability that executes a shell command with an allow-list."""

    name: str = "system.run_command"
    allowed_prefixes: Mapping[str, str] = field(
        default_factory=lambda: {"apt": "apt", "systemctl": "systemctl"}
    )

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        command = step.parameters.get("command")
        if not command:
            return ExecutionResult(step_id=step.id, status="error", error="Missing command")
        if not isinstance(command, list):
            if isinstance(command, str):
                command_list = command.split()
            else:
                return ExecutionResult(step_id=step.id, status="error", error="Command must be string or list")
        else:
            command_list = command

        if not command_list:
            return ExecutionResult(step_id=step.id, status="error", error="Command is empty")

        executable = command_list[0]
        if not any(executable.startswith(prefix) for prefix in self.allowed_prefixes.values()):
            return ExecutionResult(
                step_id=step.id,
                status="blocked",
                error=f"Executable '{executable}' not in allow list",
            )

        try:
            completed = subprocess.run(
                command_list,
                capture_output=True,
                check=False,
                text=True,
            )
        except FileNotFoundError:
            return ExecutionResult(
                step_id=step.id,
                status="error",
                error=f"Command '{executable}' not found",
            )

        status = "success" if completed.returncode == 0 else "error"
        output = completed.stdout.strip()
        error = completed.stderr.strip() or None
        return ExecutionResult(step_id=step.id, status=status, output=output or None, error=error)


@dataclass
class BlueprintCapability:
    """Capability that renders blueprint files to disk for later execution."""

    name: str = "automation.write_blueprint"
    output_dir: Path = Path.home() / ".ainux" / "blueprints"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        blueprint_name = step.parameters.get("name") or step.id
        contents = step.parameters.get("contents")
        if contents is None:
            return ExecutionResult(step_id=step.id, status="error", error="No blueprint contents supplied")
        path = self.output_dir.expanduser()
        path.mkdir(parents=True, exist_ok=True)
        target = path / f"{blueprint_name}.yaml"
        if isinstance(contents, (dict, list)):
            serialized = json.dumps(contents, indent=2, ensure_ascii=False)
        else:
            serialized = str(contents)
        target.write_text(serialized, encoding="utf-8")
        return ExecutionResult(
            step_id=step.id,
            status="success",
            output=str(target),
        )


@dataclass
class PointerControlCapability:
    """Capability that executes pointer automation actions."""

    name: str = "ui.control_pointer"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        operation = str(params.get("operation") or "move")

        if pyautogui is None:
            return ExecutionResult(
                step_id=step.id,
                status="blocked",
                error="Pointer control requires the 'pyautogui' package",
            )

        try:
            if operation == "move":
                dx = int(params.get("dx") or 0)
                dy = int(params.get("dy") or 0)
                duration = float(params.get("duration") or 0.0)
                pyautogui.FAILSAFE = False
                pyautogui.moveRel(dx, dy, duration=max(duration, 0.0))
                output = f"moved pointer by ({dx}, {dy})"
            elif operation == "click":
                button = str(params.get("button") or "left")
                clicks = int(params.get("clicks") or 1)
                interval = float(params.get("interval") or 0.0)
                pyautogui.FAILSAFE = False
                pyautogui.click(button=button, clicks=max(clicks, 1), interval=max(interval, 0.0))
                output = f"clicked {button} x{clicks}"
            else:
                return ExecutionResult(
                    step_id=step.id,
                    status="error",
                    error=f"Unsupported pointer operation '{operation}'",
                )
        except Exception as exc:  # pragma: no cover - runtime guard
            return ExecutionResult(step_id=step.id, status="error", error=str(exc))

        return ExecutionResult(step_id=step.id, status="success", output=output)
