"""Plan generation for the Ainux natural language orchestrator."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..client import ChatClient, ChatClientError
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

        if action == "system.optimize_resources":
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
        elif action == "ui.control_pointer":
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
            steps.append(
                PlanStep(
                    id="gather_context",
                    action="ui.collect_user_context",
                    description="Gather current desktop state and user goal for guidance.",
                    parameters=parameters,
                )
            )
        elif action == "system.launch_application":
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
                    id="capture_pointer_state",
                    action="ui.collect_user_context",
                    description="Capture current pointer position and focused surface for safety.",
                    parameters={"focus": "pointer"},
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
            steps.append(
                PlanStep(
                    id="launch_application",
                    action="system.launch_application",
                    description="Launch the requested desktop application using Ubuntu defaults.",
                    parameters=parameters,
                )
            )
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
            low_level_parameters = self._prepare_low_level_parameters(parameters)
            steps.append(
                PlanStep(
                    id="compile_and_run",
                    action="system.execute_low_level",
                    description="Compile and execute the provided low-level program snippet.",
                    parameters=low_level_parameters,
                )
            )
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

    # -- Low-level orchestration helpers -------------------------------------------------

    def _prepare_low_level_parameters(self, parameters: Dict[str, object]) -> Dict[str, object]:
        """Ensure low-level execution requests include compilable source code."""

        params = dict(parameters)
        raw_source = params.get("source") or params.get("code")
        if isinstance(raw_source, str) and raw_source.strip():
            return params
        if raw_source and not isinstance(raw_source, str):
            return params

        request = str(params.get("original_request") or "").strip()
        if not request:
            return params

        target = self._infer_low_level_target(request)
        if not target:
            return params

        executable, extra_args = target
        language = str(params.get("language") or "assembly").lower()

        if language in {"asm", "assembly"}:
            params["language"] = "assembly"
            params["source"] = self._generate_assembly_launcher(executable, extra_args)
        elif language in {"machine", "binary"}:
            # Machine code requests fall back to an auto-generated assembly stub that
            # launches the requested program. This keeps the workflow transparent while
            # still producing an executable artifact the runtime can compile.
            params["language"] = "assembly"
            params["source"] = self._generate_assembly_launcher(executable, extra_args)
        else:
            params["language"] = "c"
            params["source"] = self._generate_c_launcher(executable, extra_args)

        return params

    def _infer_low_level_target(self, request: str) -> Optional[Tuple[str, List[str]]]:
        """Return an executable path inferred from the natural-language *request*."""

        lowered = request.lower()

        # Explicit keyword mapping for common desktop applications.
        keyword_targets = {
            "firefox": (["firefox", "/usr/bin/firefox"], "/usr/bin/firefox"),
            "terminal": (
                [
                    "gnome-terminal",
                    "x-terminal-emulator",
                    "/usr/bin/gnome-terminal",
                    "xfce4-terminal",
                ],
                "/usr/bin/gnome-terminal",
            ),
            "gnome-terminal": (["gnome-terminal", "/usr/bin/gnome-terminal"], "/usr/bin/gnome-terminal"),
            "chrome": (["google-chrome", "/usr/bin/google-chrome"], "/usr/bin/google-chrome"),
            "chromium": (["chromium-browser", "chromium", "/usr/bin/chromium"], "/usr/bin/chromium"),
            "code": (["code", "/usr/bin/code"], "/usr/bin/code"),
        }

        for keyword, (candidates, fallback) in keyword_targets.items():
            if keyword in lowered:
                resolved = self._resolve_executable(candidates) or fallback
                if resolved:
                    return resolved, []

        # Fall back to scanning tokens around common verbs such as "run" or "execute".
        command_match = re.search(
            r"(?:execute|excute|run|launch|start|open|실행|열어|켜)\s+([\w.-]+)",
            lowered,
        )
        if command_match:
            candidate = command_match.group(1)
            resolved = self._resolve_executable([candidate])
            if not resolved:
                resolved = self._default_executable(candidate)
            if resolved:
                return resolved, []

        # As a last resort, scan every token and attempt to resolve it as an executable
        # while skipping common filler words.
        skip_tokens = {
            "assembly",
            "asm",
            "machine",
            "code",
            "by",
            "using",
            "please",
            "the",
            "this",
            "request",
            "program",
            "app",
            "application",
            "어셈",
            "기계어",
            "실행",
            "열어",
            "켜",
            "줘",
            "좀",
            "으로",
            "해서",
            "excute",
        }

        for token in re.findall(r"[\w.-]+", lowered):
            if token in skip_tokens or len(token) < 2:
                continue
            resolved = self._resolve_executable([token])
            if not resolved:
                resolved = self._default_executable(token)
            if resolved:
                return resolved, []

        return None

    def _resolve_executable(self, candidates: Sequence[str]) -> Optional[str]:
        """Resolve *candidates* to a concrete executable path."""

        for candidate in candidates:
            if not candidate:
                continue
            if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
                return candidate
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    def _default_executable(self, token: str) -> Optional[str]:
        """Return a best-effort executable path for *token*."""

        token = token.strip()
        if not token or "/" in token:
            return None
        return f"/usr/bin/{token}"

    def _generate_assembly_launcher(self, executable: str, extra_args: Sequence[str]) -> str:
        """Generate an x86_64 assembly stub that launches *executable*."""

        args = [executable, *extra_args]
        escaped_strings = [self._escape_assembly_string(value) for value in args]

        lines = [
            ".section .text",
            ".global _start",
            "_start:",
            "    xor %rdx, %rdx",
            "    mov $59, %rax",
            "    lea cmd_path(%rip), %rdi",
            "    lea argv_list(%rip), %rsi",
            "    syscall",
            "    mov $60, %rax",
            "    xor %rdi, %rdi",
            "    syscall",
            "",
            ".section .rodata",
            "cmd_path:",
            f"    .string \"{escaped_strings[0]}\"",
            "argv_list:",
        ]

        for index in range(len(args)):
            if index == 0:
                lines.append("    .quad cmd_path")
            else:
                lines.append(f"    .quad arg_{index}")
        lines.append("    .quad 0")

        for index, value in enumerate(escaped_strings[1:], start=1):
            lines.append("")
            lines.append(f"arg_{index}:")
            lines.append(f"    .string \"{value}\"")

        lines.append("")
        return "\n".join(lines)

    def _generate_c_launcher(self, executable: str, extra_args: Sequence[str]) -> str:
        """Generate a small C program that launches *executable*."""

        args = [executable, *extra_args]
        escaped = [value.replace("\\", "\\\\").replace('"', '\\"') for value in args]

        args_initializer = ", ".join(f'"{value}"' for value in escaped)

        lines = [
            "#include <errno.h>",
            "#include <string.h>",
            "#include <unistd.h>",
            "#include <stdio.h>",
            "",
            "int main(void) {",
            f"    const char *args[] = {{{args_initializer}, NULL}};",
            f"    execvp(\"{escaped[0]}\", (char * const *)args);",
            "    perror(\"execvp\");",
            "    return errno ? (int)errno : 1;",
            "}",
        ]

        return "\n".join(lines)

    @staticmethod
    def _escape_assembly_string(value: str) -> str:
        """Escape a literal for inclusion in an assembly .string directive."""

        return value.replace("\\", "\\\\").replace('"', '\\"')

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
        next_steps = [step for step in plan.steps if step.id not in completed_ids]
        message: Optional[str] = None
        if history:
            last = history[-1]
            message = last.output or last.error
        return PlanReview(plan=plan, next_steps=next_steps, complete=not next_steps, message=message)

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
