"""Plan generation for the Ainux natural language orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

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

        if action == "system.optimize_resources":
            steps.append(
                PlanStep(
                    id="collect_metrics",
                    action="system.collect_resource_metrics",
                    description="Collect CPU, memory, and IO usage to understand current load.",
                    parameters={},
                )
            )
            steps.append(
                PlanStep(
                    id="analyze_hotspots",
                    action="system.analyze_resource_hotspots",
                    description="Analyze metrics to identify processes or services causing pressure.",
                    parameters={},
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
                    parameters=parameters,
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
