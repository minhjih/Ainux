"""Plan generation for the Ainux natural language orchestrator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..client import ChatClient, ChatClientError
from .models import ActionPlan, Intent, PlanStep


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
    "`hardware.provision_gpu_stack`, `orchestration.schedule_maintenance`, or\n"
    "`network.configure`."
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
        steps_payload = payload.get("steps") or []
        notes = payload.get("notes")
        steps: List[PlanStep] = []
        for index, step_payload in enumerate(steps_payload, 1):
            step_id = str(step_payload.get("id") or f"step_{index}")
            steps.append(
                PlanStep(
                    id=step_id,
                    action=str(step_payload.get("action") or intent.action or "free_form"),
                    description=str(step_payload.get("description") or ""),
                    parameters=step_payload.get("parameters") or {},
                    depends_on=list(step_payload.get("depends_on") or []),
                )
            )
        return ActionPlan(intent=intent, steps=steps, notes=str(notes) if notes else None)

    def _heuristic_plan(self, intent: Intent, context: Dict[str, object]) -> ActionPlan:
        steps: List[PlanStep] = []
        action = intent.action
        parameters = dict(intent.parameters)

        if action == "hardware.provision_gpu_stack":
            steps.append(
                PlanStep(
                    id="detect_hardware",
                    action="inventory.collect_gpu_metadata",
                    description="Inspect GPU hardware and installed drivers.",
                    parameters={},
                )
            )
            steps.append(
                PlanStep(
                    id="select_versions",
                    action="hardware.select_driver_combo",
                    description="Select validated driver/CUDA versions for the detected GPUs.",
                    parameters={},
                    depends_on=["detect_hardware"],
                )
            )
            steps.append(
                PlanStep(
                    id="apply_stack",
                    action="hardware.apply_gpu_stack",
                    description="Install or update GPU drivers and CUDA packages.",
                    parameters=parameters,
                    depends_on=["select_versions"],
                )
            )
        elif action == "orchestration.schedule_maintenance":
            steps.append(
                PlanStep(
                    id="gather_targets",
                    action="scheduler.collect_targets",
                    description="Collect nodes and services affected by the maintenance window.",
                    parameters={},
                )
            )
            steps.append(
                PlanStep(
                    id="draft_plan",
                    action="scheduler.create_window",
                    description="Draft the maintenance schedule and change plan.",
                    parameters=parameters,
                    depends_on=["gather_targets"],
                )
            )
            steps.append(
                PlanStep(
                    id="notify",
                    action="communications.broadcast",
                    description="Notify stakeholders and request approvals.",
                    parameters={"channels": ["email", "slack"]},
                    depends_on=["draft_plan"],
                )
            )
        elif action == "network.configure":
            steps.append(
                PlanStep(
                    id="audit_rules",
                    action="network.audit_state",
                    description="Collect current firewall and QoS rules.",
                    parameters={},
                )
            )
            steps.append(
                PlanStep(
                    id="apply_rules",
                    action="network.apply_changes",
                    description="Apply requested network and packet configuration changes.",
                    parameters=parameters,
                    depends_on=["audit_rules"],
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
