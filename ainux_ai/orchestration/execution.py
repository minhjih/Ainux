"""Execution runtime for orchestration plans."""

from __future__ import annotations

import json
import subprocess
import getpass
import os
import shlex
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Protocol

try:  # pragma: no cover - optional runtime dependency
    import pyautogui  # type: ignore
except Exception:  # pragma: no cover - defensive fallback
    pyautogui = None

from .low_level import prepare_low_level_parameters
from .models import ExecutionResult, PlanStep


def _gather_process_table(limit: int = 10) -> List[Dict[str, object]]:
    """Return a list of running processes sorted by CPU usage."""

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,comm,%cpu,%mem,user", "--sort=-%cpu"],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return []

    table: List[Dict[str, object]] = []
    for line in result.stdout.strip().splitlines()[1:limit + 1]:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, command, cpu, mem, user = parts
        try:
            entry = {
                "pid": int(pid),
                "command": command,
                "cpu": float(cpu),
                "memory": float(mem),
                "user": user,
            }
        except ValueError:
            continue
        table.append(entry)
    return table


def _parse_memory_snapshot() -> Dict[str, float]:
    """Return memory information gathered from ``free`` if available."""

    snapshot: Dict[str, float] = {}
    try:
        result = subprocess.run(
            ["free", "-m"],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return snapshot

    lines = [line for line in result.stdout.strip().splitlines() if line]
    if len(lines) < 2:
        return snapshot

    headers = lines[0].split()
    values = lines[1].split()
    if len(headers) != len(values):
        return snapshot

    for header, value in zip(headers[1:], values[1:]):
        try:
            snapshot[header.lower()] = float(value)
        except ValueError:
            continue
    return snapshot


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
class CollectResourceMetricsCapability:
    """Collect CPU, memory, and process metrics from the running system."""

    name: str = "system.collect_resource_metrics"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        metrics: Dict[str, object] = {}
        params = step.parameters or {}

        try:
            load1, load5, load15 = os.getloadavg()
            metrics["load_average"] = {"1m": load1, "5m": load5, "15m": load15}
        except (AttributeError, OSError):
            metrics["load_average"] = None

        metrics["processes"] = _gather_process_table(limit=int(params.get("limit", 10)))
        metrics["memory"] = _parse_memory_snapshot()

        return ExecutionResult(
            step_id=step.id,
            status="success",
            output=json.dumps(metrics, ensure_ascii=False),
        )


@dataclass
class AnalyzeResourceHotspotsCapability:
    """Inspect current processes and highlight likely resource hotspots."""

    name: str = "system.analyze_resource_hotspots"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        processes = _gather_process_table(limit=int(params.get("limit", 10)))
        if not processes:
            return ExecutionResult(
                step_id=step.id,
                status="blocked",
                error="Process information unavailable",
            )

        cpu_threshold = float(params.get("cpu_threshold", 40.0))
        mem_threshold = float(params.get("memory_threshold", 30.0))

        cpu_hotspots = [proc for proc in processes if proc["cpu"] >= cpu_threshold]
        mem_hotspots = [proc for proc in processes if proc["memory"] >= mem_threshold]

        analysis = {
            "cpu_hotspots": cpu_hotspots or processes[:3],
            "memory_hotspots": mem_hotspots or sorted(processes, key=lambda p: p["memory"], reverse=True)[:3],
            "thresholds": {"cpu": cpu_threshold, "memory": mem_threshold},
        }

        return ExecutionResult(
            step_id=step.id,
            status="success",
            output=json.dumps(analysis, ensure_ascii=False),
        )


@dataclass
class ApplyResourceTuningCapability:
    """Apply light-weight tuning such as renicing the most expensive process."""

    name: str = "system.apply_resource_tuning"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        target_pid = params.get("pid") or params.get("target_pid")
        preferred_user = params.get("user") or getpass.getuser()
        nice_value = int(params.get("nice", 10))

        if nice_value < -20 or nice_value > 19:
            return ExecutionResult(
                step_id=step.id,
                status="error",
                error="Nice value must be between -20 and 19",
            )

        processes = _gather_process_table(limit=25)
        if not target_pid:
            for proc in processes:
                if proc["user"] == preferred_user:
                    target_pid = proc["pid"]
                    break

        if not target_pid:
            return ExecutionResult(
                step_id=step.id,
                status="blocked",
                error="No suitable process found for tuning",
            )

        try:
            target_pid_int = int(target_pid)
        except (TypeError, ValueError):
            return ExecutionResult(step_id=step.id, status="error", error="Invalid PID supplied")

        try:
            completed = subprocess.run(
                ["renice", str(nice_value), "-p", str(target_pid_int)],
                capture_output=True,
                check=False,
                text=True,
            )
        except FileNotFoundError:
            return ExecutionResult(step_id=step.id, status="error", error="'renice' command not available")

        status = "success" if completed.returncode == 0 else "error"
        output = completed.stdout.strip() or None
        error = completed.stderr.strip() or None
        return ExecutionResult(step_id=step.id, status=status, output=output, error=error)


@dataclass
class ApplicationLauncherCapability:
    """Capability that launches common desktop applications such as terminals."""

    name: str = "system.launch_application"
    terminal_commands: Iterable[Iterable[str]] = field(
        default_factory=lambda: (
            ("gnome-terminal",),
            ("x-terminal-emulator",),
            ("xfce4-terminal",),
            ("konsole",),
            ("tilix",),
            ("mate-terminal",),
            ("lxterminal",),
            ("xterm",),
        )
    )

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        command = params.get("command")
        target = str(params.get("target") or "").lower()

        candidates: List[List[str]] = []

        if command:
            if isinstance(command, str):
                candidates.append(command.split())
            elif isinstance(command, (list, tuple)):
                candidates.append([str(part) for part in command])
            else:
                return ExecutionResult(
                    step_id=step.id,
                    status="error",
                    error="Command must be a string or list",
                )
        elif target in {"terminal", "shell", "console"}:
            candidates = [list(cmd) for cmd in self.terminal_commands]
        else:
            return ExecutionResult(
                step_id=step.id,
                status="blocked",
                error="No launch command resolved for requested target",
            )

        errors: List[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            try:
                subprocess.Popen(candidate, start_new_session=True)
            except FileNotFoundError:
                errors.append(f"Command not found: {candidate[0]}")
                continue
            except Exception as exc:  # pragma: no cover - runtime guard
                errors.append(str(exc))
                continue
            else:
                launched = " ".join(candidate)
                return ExecutionResult(
                    step_id=step.id,
                    status="success",
                    output=f"Launched application: {launched}",
                )

        if errors:
            error_message = "; ".join(errors)
        else:
            error_message = "Unable to launch application"
        return ExecutionResult(step_id=step.id, status="error", error=error_message)


@dataclass
class ProcessEnumerationCapability:
    """Enumerate processes and return a filtered snapshot."""

    name: str = "process.enumerate"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        target = str(params.get("name") or params.get("process") or "").lower()
        owner = str(params.get("user") or "").lower()
        processes = _gather_process_table(limit=int(params.get("limit", 25)))

        if target:
            processes = [proc for proc in processes if target in proc["command"].lower()]
        if owner:
            processes = [proc for proc in processes if owner in proc["user"].lower()]

        return ExecutionResult(
            step_id=step.id,
            status="success",
            output=json.dumps({"processes": processes}, ensure_ascii=False),
        )


@dataclass
class ProcessEvaluationCapability:
    """Recommend management actions for active processes."""

    name: str = "process.evaluate_actions"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        cpu_threshold = float(params.get("cpu_threshold", 60.0))
        mem_threshold = float(params.get("memory_threshold", 40.0))

        processes = _gather_process_table(limit=int(params.get("limit", 25)))
        recommendations: List[Dict[str, object]] = []

        for proc in processes:
            if proc["cpu"] >= cpu_threshold:
                recommendations.append(
                    {
                        "pid": proc["pid"],
                        "command": proc["command"],
                        "reason": f"CPU usage {proc['cpu']}% exceeds {cpu_threshold}%",
                        "suggested_action": "renice",
                        "suggested_nice": 10,
                    }
                )
            elif proc["memory"] >= mem_threshold:
                recommendations.append(
                    {
                        "pid": proc["pid"],
                        "command": proc["command"],
                        "reason": f"Memory usage {proc['memory']}% exceeds {mem_threshold}%",
                        "suggested_action": "terminate",
                        "suggested_signal": "SIGTERM",
                    }
                )

        return ExecutionResult(
            step_id=step.id,
            status="success",
            output=json.dumps({"recommendations": recommendations}, ensure_ascii=False),
        )


@dataclass
class ProcessManagementCapability:
    """Apply management operations to live processes."""

    name: str = "process.apply_management"

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        params = step.parameters or {}
        action = str(params.get("action") or params.get("suggested_action") or "renice").lower()
        pid = params.get("pid") or params.get("target_pid")
        if pid is None:
            target_name = params.get("name") or params.get("process")
            if target_name:
                for proc in _gather_process_table(limit=50):
                    if target_name in proc["command"]:
                        pid = proc["pid"]
                        break
        if pid is None:
            return ExecutionResult(step_id=step.id, status="blocked", error="No target process supplied")

        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return ExecutionResult(step_id=step.id, status="error", error="Invalid PID supplied")

        if action in {"terminate", "kill", "signal"}:
            signal_name = str(params.get("signal") or params.get("suggested_signal") or "SIGTERM")
            try:
                sig = getattr(signal, signal_name)
            except AttributeError:
                return ExecutionResult(step_id=step.id, status="error", error=f"Unknown signal '{signal_name}'")
            try:
                os.kill(pid_int, sig)
            except PermissionError as exc:
                return ExecutionResult(step_id=step.id, status="error", error=str(exc))
            except ProcessLookupError:
                return ExecutionResult(step_id=step.id, status="error", error="Process no longer exists")
            return ExecutionResult(
                step_id=step.id,
                status="success",
                output=f"Sent {signal_name} to PID {pid_int}",
            )

        if action in {"pause", "suspend"}:
            try:
                os.kill(pid_int, signal.SIGSTOP)
            except PermissionError as exc:
                return ExecutionResult(step_id=step.id, status="error", error=str(exc))
            except ProcessLookupError:
                return ExecutionResult(step_id=step.id, status="error", error="Process no longer exists")
            return ExecutionResult(step_id=step.id, status="success", output=f"Paused PID {pid_int}")

        if action in {"resume", "continue"}:
            try:
                os.kill(pid_int, signal.SIGCONT)
            except PermissionError as exc:
                return ExecutionResult(step_id=step.id, status="error", error=str(exc))
            except ProcessLookupError:
                return ExecutionResult(step_id=step.id, status="error", error="Process no longer exists")
            return ExecutionResult(step_id=step.id, status="success", output=f"Resumed PID {pid_int}")

        if action == "renice":
            nice_value = int(params.get("nice") or params.get("suggested_nice") or 5)
            if nice_value < -20 or nice_value > 19:
                return ExecutionResult(step_id=step.id, status="error", error="Nice value must be between -20 and 19")
            try:
                completed = subprocess.run(
                    ["renice", str(nice_value), "-p", str(pid_int)],
                    capture_output=True,
                    check=False,
                    text=True,
                )
            except FileNotFoundError:
                return ExecutionResult(step_id=step.id, status="error", error="'renice' command not available")
            status = "success" if completed.returncode == 0 else "error"
            return ExecutionResult(
                step_id=step.id,
                status=status,
                output=completed.stdout.strip() or None,
                error=(completed.stderr.strip() or (f"Process exited with status {completed.returncode}" if status == "error" else None)),
            )

        return ExecutionResult(step_id=step.id, status="blocked", error=f"Unsupported process action '{action}'")


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


@dataclass
class LowLevelCodeCapability:
    """Compile and execute low-level snippets such as C or assembly."""

    name: str = "system.execute_low_level"
    timeout: int = 10

    def execute(self, step: PlanStep, context: Optional[Dict[str, object]] = None) -> ExecutionResult:
        raw_params = step.parameters or {}
        if isinstance(raw_params, dict):
            base_params = dict(raw_params)
        else:
            base_params = {}
        normalized = prepare_low_level_parameters(base_params)
        if isinstance(step.parameters, dict):
            step.parameters.clear()
            step.parameters.update(normalized)
        params = dict(normalized)

        source = params.get("source") or params.get("code")
        if not source or not str(source).strip():
            hint = params.get("original_request") or step.description or step.action
            return ExecutionResult(
                step_id=step.id,
                status="blocked",
                error=f"Missing source code for {hint}",
            )

        language = str(params.get("language") or "c").lower()
        args = params.get("args") or []
        if isinstance(args, str):
            args = shlex.split(args)
        elif isinstance(args, (list, tuple)):
            args = [str(item) for item in args]
        else:
            return ExecutionResult(step_id=step.id, status="error", error="args must be a list or string")

        source_text = str(source)
        with tempfile.TemporaryDirectory(prefix="ainux-lowlevel-") as tmpdir:
            workdir = Path(tmpdir)
            binary_path = workdir / "program"

            if language in {"c", "c11", "c99"}:
                source_path = workdir / "program.c"
                source_path.write_text(source_text, encoding="utf-8")
                compile_cmd = ["gcc", "-std=c11", "-O2", str(source_path), "-o", str(binary_path)]
            elif language in {"asm", "assembly"}:
                source_path = workdir / "program.s"
                source_path.write_text(source_text, encoding="utf-8")
                compile_cmd = ["gcc", "-nostdlib", "-no-pie", "-Wl,-e,_start", "-x", "assembler", str(source_path), "-o", str(binary_path)]
            elif language in {"machine", "binary"}:
                try:
                    binary_path.write_bytes(bytes.fromhex(source_text))
                except ValueError:
                    return ExecutionResult(step_id=step.id, status="error", error="Machine code must be a hex string")
                binary_path.chmod(0o700)
                compile_cmd = None
            else:
                return ExecutionResult(
                    step_id=step.id,
                    status="error",
                    error=f"Unsupported language '{language}'",
                )

            if compile_cmd:
                try:
                    compiled = subprocess.run(
                        compile_cmd,
                        capture_output=True,
                        check=False,
                        text=True,
                    )
                except FileNotFoundError:
                    return ExecutionResult(step_id=step.id, status="error", error="gcc compiler not available")

                if compiled.returncode != 0:
                    return ExecutionResult(
                        step_id=step.id,
                        status="error",
                        error=compiled.stderr.strip() or "Compilation failed",
                    )

            try:
                completed = subprocess.run(
                    [str(binary_path), *list(args)],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=self.timeout,
                )
            except FileNotFoundError:
                return ExecutionResult(step_id=step.id, status="error", error="Compiled binary missing")
            except subprocess.TimeoutExpired:
                return ExecutionResult(step_id=step.id, status="error", error="Execution timed out")

            status = "success" if completed.returncode == 0 else "error"
            return ExecutionResult(
                step_id=step.id,
                status=status,
                output=completed.stdout.strip() or None,
                error=(completed.stderr.strip() or (f"Process exited with status {completed.returncode}" if status == "error" else None)),
            )
