"""Safety and policy checks for orchestration plans."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..client import ChatClient, ChatClientError
from .models import ActionPlan, PlanStep, SafetyReport


_SAFETY_SYSTEM_PROMPT = (
    "You are the safety reviewer for the Ainux automation engine.\n"
    "Given a plan of actions decide which steps are allowed to execute under"
    " standard security policy. Return JSON with approved and blocked steps."
)


@dataclass
class SafetyChecker:
    """Validate plans before execution."""

    client: Optional[ChatClient] = None
    disallowed_actions: Sequence[str] = field(default_factory=lambda: ("system.shutdown",))

    def review(self, plan: ActionPlan, context: Optional[Dict[str, object]] = None) -> SafetyReport:
        context = context or {}
        report = self._baseline_report(plan)
        if self.client:
            try:
                model_report = self._review_with_model(plan, context)
                return self._merge_reports(report, model_report)
            except (ChatClientError, ValueError, json.JSONDecodeError):
                pass
        return report

    def _baseline_report(self, plan: ActionPlan) -> SafetyReport:
        blocked: List[PlanStep] = []
        approved: List[PlanStep] = []
        warnings: List[str] = []
        for step in plan.steps:
            if step.action in self.disallowed_actions:
                blocked.append(step)
                warnings.append(f"Step {step.id} uses disallowed action {step.action}.")
            else:
                approved.append(step)
        return SafetyReport(approved_steps=approved, blocked_steps=blocked, warnings=warnings)

    def _review_with_model(self, plan: ActionPlan, context: Dict[str, object]) -> SafetyReport:
        payload = {
            "plan": [
                {
                    "id": step.id,
                    "action": step.action,
                    "description": step.description,
                    "parameters": step.parameters,
                }
                for step in plan.steps
            ],
            "context": context,
        }
        messages = [
            {"role": "system", "content": _SAFETY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        completion = self.client.create_chat_completion(
            messages,
            response_format={"type": "json_object"},
            extra_options={"seed": 3},
        )
        payload = json.loads(completion.content)
        blocked_ids = set(payload.get("blocked_steps") or [])
        warnings = list(payload.get("warnings") or [])
        rationale = payload.get("rationale")
        approved: List[PlanStep] = []
        blocked: List[PlanStep] = []
        for step in plan.steps:
            if step.id in blocked_ids:
                blocked.append(step)
            else:
                approved.append(step)
        return SafetyReport(
            approved_steps=approved,
            blocked_steps=blocked,
            warnings=warnings,
            rationale=str(rationale) if rationale else None,
        )

    def _merge_reports(self, baseline: SafetyReport, extra: SafetyReport) -> SafetyReport:
        baseline_blocked = {step.id: step for step in baseline.blocked_steps}
        baseline_approved = {step.id: step for step in baseline.approved_steps}

        for step in extra.blocked_steps:
            baseline_blocked[step.id] = step
            baseline_approved.pop(step.id, None)

        for step in extra.approved_steps:
            if step.id not in baseline_blocked:
                baseline_approved[step.id] = step

        warnings = list({*baseline.warnings, *(extra.warnings or [])})
        rationale = extra.rationale or baseline.rationale
        return SafetyReport(
            approved_steps=list(baseline_approved.values()),
            blocked_steps=list(baseline_blocked.values()),
            warnings=warnings,
            rationale=rationale,
        )
