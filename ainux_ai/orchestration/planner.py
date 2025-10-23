"""Plan generation for the Ainux natural language orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..client import ChatClient, ChatClientError
from .low_level import prepare_low_level_parameters
from .models import ActionPlan, ExecutionResult, Intent, PlanReview, PlanStep


_PLANNER_SYSTEM_PROMPT = (
    "You are the orchestration planner for the Ainux operating system.\n"
    "Given a normalized intent you must create a deterministic plan of actions"
    " the automation engine can execute.\n\n"
    "Respond as JSON with the following structure:\n"
    "{\n"
    "  \"steps\": [\n"
    "    {\n"
    "      \"id\": string,\n"
    "      \"action\": string,\n"
    "      \"description\": string,\n"
    "      \"parameters\": object,\n"
    "      \"depends_on\": [string]\n"
    "    }\n"
    "  ],\n"
    "  \"notes\": string\n"
    "}\n\n"
    "Choose deterministic actions that map to Ainux capabilities such as\n"
    "`system.collect_resource_metrics`, `process.apply_management`,\n"
    "`ui.present_walkthrough`, `system.run_command`, or\n"
    "`automation.write_blueprint`."
)

_REVIEW_SYSTEM_PROMPT = (
    "You are the orchestration planner for the Ainux operating system.\n"
    "You receive execution feedback after each step.\n"
    "Update the plan and decide the next deterministic actions.\n\n"
    "Respond as JSON with optional updated plan, remaining next_steps,"
    " completion flag, and an operator-facing message."
)


@dataclass
class Planner:
    """Transform intents into ordered execution plans."""

    client: Optional[ChatClient] = None

    def create_plan(self, intent: Intent, context: Optional[Dict[str, object]] = None) -> ActionPlan:
        context = context or {}
        if self.client:
            try:
                return self._plan_with_model(intent, context)
            except (ChatClientError, ValueError, json.JSONDecodeError):
                pass
        return self._heuristic_plan(intent, context)

    def _plan_with_model(self, intent: Intent, context: Dict[str, object]) -> ActionPlan:
        payload = {
            "intent": {
                "action": intent.action,
                "parameters": intent.parameters,
                "confidence": intent.confidence,
            },
            "context": context,
        }
        messages = [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        completion = self.client.create_chat_completion(
            messages,
            response_format={"type": "json_object"},
            extra_options={"seed": 2},
        )
        payload = json.loads(completion.content)
        steps = self._parse_steps(intent, payload.get("steps") or [])
        notes = payload.get("notes")
        return ActionPlan(intent=intent, steps=steps, notes=str(notes) if notes else None)

    def _heuristic_plan(self, intent: Intent, context: Dict[str, object]) -> ActionPlan:
        steps: List[PlanStep] = []
        action = intent.action
        parameters = dict(intent.parameters)
        parameters.setdefault("original_request", intent.raw_input)

        if self._looks_like_application_launch(parameters) and action in {
            "ui.assist_user",
            "analysis.review_request",
        }:
            steps.append(
                self._build_launch_step(parameters, intent.raw_input)
            )
        elif action == "system.optimize_resources":
            steps.append(
                PlanStep(
                    id="collect_metrics",
                    action="system.collect_resource_metrics",
                    description="Collect CPU, memory, and IO usage to understand current load.",
                    parameters={"limit": parameters.get("limit", 10), "original_request": intent.raw_input},
                )
            )
            steps.append(
                PlanStep(
                    id="analyze_hotspots",
                    action="system.analyze_resource_hotspots",
                    description="Analyze metrics to identify processes or services causing pressure.",
                    parameters={
                        "cpu_threshold": parameters.get("cpu_threshold", 40.0),
                        "memory_threshold": parameters.get("memory_threshold", 30.0),
                        "original_request": intent.raw_input,
                    },
                    depends_on=["collect_metrics"],
                )
            )
            steps.append(
                PlanStep(
                    id="apply_tuning",
                    action="system.apply_resource_tuning",
                    description="Apply scheduling or limit adjustments to balance resource usage.",
                    parameters=parameters,
                    depends_on=["analyze_hotspots"],
                )
            )
        elif action == "process.manage":
            steps.append(
                PlanStep(
                    id="list_processes",
                    action="process.enumerate",
                    description="List relevant processes and capture their current state.",
                    parameters={"limit": parameters.get("limit", 25), **parameters},
                )
            )
            steps.append(
                PlanStep(
                    id="evaluate_process_actions",
                    action="process.evaluate_actions",
                    description="Decide whether to reprioritize, pause, or terminate processes.",
                    parameters=parameters,
                    depends_on=["list_processes"],
                )
            )
            steps.append(
                PlanStep(
                    id="apply_process_change",
                    action="process.apply_management",
                    description="Perform the selected process management operations.",
                    parameters=parameters,
                    depends_on=["evaluate_process_actions"],
                )
            )
        elif action == "ui.assist_user":
            if self._looks_like_application_launch(parameters):
                steps.append(self._build_launch_step(parameters, intent.raw_input))
            else:
                steps.append(
                    PlanStep(
                        id="gather_context",
                        action="ui.collect_user_context",
                        description="Gather current desktop state and user goal for guidance.",
                        parameters=parameters,
                    )
                )
                steps.append(
                    PlanStep(
                        id="present_walkthrough",
                        action="ui.present_walkthrough",
                        description="Prepare a walkthrough describing how to accomplish the task in the UI.",
                        parameters=parameters,
                        depends_on=["gather_context"],
                    )
                )
                steps.append(
                    PlanStep(
                        id="queue_actions",
                        action="ui.queue_actions",
                        description="Queue any scripted clicks or commands the assistant can trigger on behalf of the user.",
                        parameters=parameters,
                        depends_on=["present_walkthrough"],
                    )
                )
        elif action == "ui.control_pointer":
            steps.append(
                PlanStep(
                    id="ensure_pointer_dependencies",
                    action="system.ensure_python_package",
                    description="Ensure the pointer automation dependency is installed.",
                    parameters={
                        "package": "pyautogui",
                        "module": "pyautogui",
                        "original_request": intent.raw_input,
                    },
                )
            )
            steps.append(
                PlanStep(
                    id="capture_pointer_state",
                    action="ui.collect_user_context",
                    description="Capture current pointer position and focused surface for safety.",
                    parameters={"focus": "pointer"},
                    depends_on=["ensure_pointer_dependencies"],
                )
            )
            steps.append(
                PlanStep(
                    id="apply_pointer_action",
                    action="ui.control_pointer",
                    description="Apply the requested pointer movement or click on behalf of the user.",
                    parameters=parameters,
                    depends_on=["capture_pointer_state"],
                )
            )
        elif action == "system.launch_application":
            steps.append(self._build_launch_step(parameters, intent.raw_input))
        elif action == "system.schedule_task":
            steps.append(
                PlanStep(
                    id="collect_requirements",
                    action="system.collect_task_requirements",
                    description="Collect timing preferences and resource constraints for the task.",
                    parameters=parameters,
                )
            )
            steps.append(
                PlanStep(
                    id="draft_schedule",
                    action="scheduler.create_task_schedule",
                    description="Draft a schedule or cron entry that satisfies the requirements.",
                    parameters=parameters,
                    depends_on=["collect_requirements"],
                )
            )
            steps.append(
                PlanStep(
                    id="publish_guidance",
                    action="scheduler.publish_user_guidance",
                    description="Share the resulting schedule and any follow-up actions with the user.",
                    parameters=parameters,
                    depends_on=["draft_schedule"],
                )
            )
        elif action == "system.update":
            steps.append(
                PlanStep(
                    id="refresh_package_index",
                    action="system.run_command",
                    description="Update the package index to pull the latest metadata.",
                    parameters={"command": ["apt", "update"]},
                )
            )
            steps.append(
                PlanStep(
                    id="apply_updates",
                    action="system.run_command",
                    description="Apply available system updates in non-interactive mode.",
                    parameters={"command": ["apt", "upgrade", "-y"]},
                    depends_on=["refresh_package_index"],
                )
            )
        elif action == "system.execute_low_level":
            raw_source = parameters.get("source") or parameters.get("code")
            low_level_parameters = prepare_low_level_parameters(parameters)
            metadata = (
                low_level_parameters.get("_ainux_low_level")
                if isinstance(low_level_parameters, dict)
                else None
            )
            inspect_dep: List[str] = []
            if not (isinstance(raw_source, str) and raw_source.strip()):
                inspect_params = {
                    "target": None,
                    "candidate": None,
                    "original_request": intent.raw_input,
                }
                if isinstance(metadata, dict):
                    candidate = metadata.get("candidate")
                    target_info = metadata.get("target") or {}
                    if isinstance(candidate, str) and candidate.strip():
                        inspect_params["target"] = candidate.strip()
                        inspect_params["candidate"] = candidate.strip()
                    elif isinstance(target_info, dict):
                        executable = target_info.get("executable")
                        if isinstance(executable, str) and executable.strip():
                            inspect_params["target"] = executable.strip()
                    if metadata:
                        inspect_params["_ainux_low_level"] = metadata
                else:
                    candidate = parameters.get("target") or parameters.get("program")
                    if isinstance(candidate, str) and candidate.strip():
                        inspect_params["target"] = candidate.strip()

                filtered_params = {
                    key: value
                    for key, value in inspect_params.items()
                    if value not in (None, "")
                }
                steps.append(
                    PlanStep(
                        id="inspect_command",
                        action="system.inspect_command",
                        description="Collect details about the requested executable before generating code.",
                        parameters=filtered_params,
                    )
                )
                inspect_dep.append("inspect_command")
            steps.append(
                PlanStep(
                    id="compile_and_run",
                    action="system.execute_low_level",
                    description="Compile and execute the provided low-level program snippet.",
                    parameters=low_level_parameters,
                    depends_on=inspect_dep,
                )
            )
        else:
            if self._looks_like_application_launch(parameters):
                steps.append(self._build_launch_step(parameters, intent.raw_input))
            else:
                steps.append(
                    PlanStep(
                        id="analyze",
                        action=action or "analysis.review_request",
                        description="Analyze request and prepare manual runbook.",
                        parameters=parameters,
                    )
                )
        return ActionPlan(intent=intent, steps=steps, notes="Generated by heuristic planner")

    def review_execution(
        self,
        intent: Intent,
        plan: ActionPlan,
        history: List[ExecutionResult],
        context: Optional[Dict[str, object]] = None,
    ) -> PlanReview:
        context = context or {}
        if self.client:
            try:
                return self._review_with_model(intent, plan, history, context)
            except (ChatClientError, ValueError, json.JSONDecodeError):
                pass
        return self._heuristic_review(plan, history)

    def _review_with_model(
        self,
        intent: Intent,
        plan: ActionPlan,
        history: List[ExecutionResult],
        context: Dict[str, object],
    ) -> PlanReview:
        payload = {
            "intent": {
                "action": intent.action,
                "parameters": intent.parameters,
                "confidence": intent.confidence,
            },
            "plan": [
                {
                    "id": step.id,
                    "action": step.action,
                    "description": step.description,
                    "parameters": step.parameters,
                    "depends_on": step.depends_on,
                }
                for step in plan.steps
            ],
            "history": [
                {
                    "step_id": result.step_id,
                    "status": result.status,
                    "output": result.output,
                    "error": result.error,
                }
                for result in history
            ],
            "context": context,
        }
        messages = [
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        completion = self.client.create_chat_completion(
            messages,
            response_format={"type": "json_object"},
            extra_options={"seed": 4},
        )
        payload = json.loads(completion.content)
        plan_payload = payload.get("plan")
        next_steps_payload = payload.get("next_steps") or []
        message = payload.get("message")
        complete = bool(payload.get("complete"))

        if isinstance(plan_payload, dict):
            updated_steps = self._parse_steps(intent, plan_payload.get("steps") or [])
            notes = plan_payload.get("notes") or plan.notes
            updated_plan = ActionPlan(
                intent=intent,
                steps=updated_steps,
                notes=str(notes) if notes else None,
            )
        else:
            updated_plan = plan

        next_steps = self._parse_steps(intent, next_steps_payload) if next_steps_payload else [
            step for step in updated_plan.steps if step.id not in {result.step_id for result in history}
        ]

        return PlanReview(
            plan=updated_plan,
            next_steps=next_steps,
            complete=complete,
            message=str(message) if message else None,
        )

    def _heuristic_review(self, plan: ActionPlan, history: List[ExecutionResult]) -> PlanReview:
        completed_ids = {
            result.step_id
            for result in history
            if result.status not in {"blocked", "error"}
        }
        message: Optional[str] = None
        updated_plan = plan

        failure_counts: Dict[str, int] = {}
        skipped_steps: Set[str] = set()
        complete_override = False

        if history:
            last = history[-1]
            message = last.output or last.error
            dependency = self._extract_missing_dependency(last)
            if dependency and not self._plan_contains_dependency(plan, dependency):
                updated_plan = self._inject_dependency_step(plan, last.step_id, dependency)

            for result in history:
                if result.status in {"blocked", "error"}:
                    failure_counts[result.step_id] = failure_counts.get(result.step_id, 0) + 1

            if last.status in {"blocked", "error"}:
                attempts = failure_counts.get(last.step_id, 0)
                if attempts >= 3:
                    skipped_steps.add(last.step_id)
                    complete_override = True
                    if message:
                        message = f"{message} (stopped after {attempts} failures)"

        next_steps = [
            step
            for step in updated_plan.steps
            if step.id not in completed_ids and step.id not in skipped_steps
        ]

        return PlanReview(
            plan=updated_plan,
            next_steps=next_steps,
            complete=complete_override or not next_steps,
            message=message,
        )

    def _plan_contains_dependency(
        self, plan: ActionPlan, dependency: Dict[str, str]
    ) -> bool:
        dep_type = dependency.get("type") or "python"
        package = dependency.get("package")
        module = dependency.get("module") or package

        if dep_type == "system":
            command = dependency.get("command") or package
            if not command:
                return False
            for step in plan.steps:
                if step.action != "system.run_command":
                    continue
                params = step.parameters or {}
                step_command = params.get("command")
                if isinstance(step_command, str):
                    parts = step_command.split()
                elif isinstance(step_command, (list, tuple)):
                    parts = [str(part) for part in step_command]
                else:
                    continue
                if not parts:
                    continue
                executable = parts[0]
                if executable not in {"apt", "apt-get"}:
                    continue
                if "install" not in parts:
                    continue
                if command in parts:
                    return True
            return False

        for step in plan.steps:
            if step.action != "system.ensure_python_package":
                continue
            params = step.parameters or {}
            step_package = str(params.get("package") or "").strip()
            step_module = str(params.get("module") or "").strip()
            if package and step_package == package:
                return True
            if module and step_module == module:
                return True
        return False

    def _inject_dependency_step(
        self, plan: ActionPlan, failing_step_id: str, dependency: Dict[str, str]
    ) -> ActionPlan:
        dep_type = dependency.get("type") or "python"
        package = dependency.get("package") or dependency.get("module")
        module = dependency.get("module") or package
        command = dependency.get("command") or package
        if not package and not command:
            return plan

        failing_step = next(
            (step for step in plan.steps if step.id == failing_step_id),
            None,
        )

        if dep_type == "system":
            if not command:
                return plan
            ensure_id = f"install_{command.replace('.', '_').replace('-', '_')}"
            ensure_description = (
                f"Install system package '{command}' before retrying the step."
            )
            ensure_parameters = {
                "command": ["apt", "install", command, "-y"],
                "original_request": plan.intent.raw_input,
            }
            action = "system.run_command"
            depends_on = list(failing_step.depends_on) if failing_step else []
        else:
            if not package:
                return plan
            ensure_id = f"ensure_{(module or package).replace('.', '_')}"
            ensure_description = (
                f"Install required Python package '{package}' before retrying the step."
            )
            ensure_parameters = {
                "package": package,
                "module": module,
                "original_request": plan.intent.raw_input,
            }
            action = "system.ensure_python_package"
            depends_on = list(failing_step.depends_on) if failing_step else []

        ensure_step = PlanStep(
            id=ensure_id,
            action=action,
            description=ensure_description,
            parameters=ensure_parameters,
            depends_on=depends_on,
        )

        updated_steps: List[PlanStep] = []
        inserted = False
        for step in plan.steps:
            if step.id == failing_step_id and not inserted:
                updated_steps.append(ensure_step)
                inserted = True
                if failing_step:
                    new_depends = list(dict.fromkeys(step.depends_on + [ensure_id]))
                    step = PlanStep(
                        id=step.id,
                        action=step.action,
                        description=step.description,
                        parameters=step.parameters,
                        depends_on=new_depends,
                    )
            updated_steps.append(step)

        if not inserted:
            updated_steps.append(ensure_step)

        return ActionPlan(intent=plan.intent, steps=updated_steps, notes=plan.notes)

    def _extract_missing_dependency(
        self, result: ExecutionResult
    ) -> Optional[Dict[str, str]]:
        message = result.error or result.output
        if not message:
            return None

        text = message.strip()

        python_patterns = [
            r"requires the '([^']+)' package",
            r"requires the \"([^\"]+)\" package",
            r"No module named '([^']+)'",
            r'No module named \"([^\"]+)\"',
            r"ModuleNotFoundError: No module named '([^']+)'",
            r'ModuleNotFoundError: No module named \"([^\"]+)\"',
        ]

        import re

        for pattern in python_patterns:
            match = re.search(pattern, text)
            if match:
                module = match.group(1)
                package = module
                return {"type": "python", "package": package, "module": module}

        command_patterns = [
            r"Command not found: ([^;\s]+)",
            r"command not found: ([^;\s]+)",
            r"Command '([^']+)' not found",
            r"command '([^']+)' not found",
        ]

        for pattern in command_patterns:
            match = re.search(pattern, text)
            if match:
                command = match.group(1)
                return {"type": "system", "package": command, "command": command}

        return None

    def _parse_steps(self, intent: Intent, steps_payload: List[dict]) -> List[PlanStep]:
        steps: List[PlanStep] = []
        for index, step_payload in enumerate(steps_payload, 1):
            step_id = str(step_payload.get("id") or f"step_{index}")
            steps.append(
                PlanStep(
                    id=step_id,
                    action=str(
                        step_payload.get("action")
                        or intent.action
                        or "analysis.review_request"
                    ),
                    description=str(step_payload.get("description") or ""),
                    parameters=step_payload.get("parameters") or {},
                    depends_on=list(step_payload.get("depends_on") or []),
                )
            )
        return steps

    def _looks_like_application_launch(self, parameters: Dict[str, object]) -> bool:
        if not parameters:
            return False

        target = parameters.get("target") or parameters.get("application") or parameters.get("app")
        if isinstance(target, str) and target.strip():
            return True

        command = parameters.get("command")
        if isinstance(command, str) and command.strip():
            return True
        if isinstance(command, (list, tuple)):
            if any(str(part).strip() for part in command):
                return True

        operation = parameters.get("requested_operation")
        if isinstance(operation, str):
            normalized = operation.strip().lower()
            if normalized in {"execute", "excute", "run", "launch", "start", "실행", "켜줘"}:
                return bool(target)

        return False

    def _build_launch_step(self, parameters: Dict[str, object], request: str) -> PlanStep:
        launch_parameters = dict(parameters) if parameters else {}
        launch_parameters.setdefault("original_request", request)
        if "target" not in launch_parameters:
            candidate = launch_parameters.get("application") or launch_parameters.get("app")
            if isinstance(candidate, str) and candidate.strip():
                launch_parameters["target"] = candidate.strip()
        return PlanStep(
            id="launch_application",
            action="system.launch_application",
            description="Launch the requested desktop application using Ubuntu defaults.",
            parameters=launch_parameters,
        )
