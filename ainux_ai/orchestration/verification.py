"""Verification helpers that confirm whether execution satisfied the request."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..client import ChatClient, ChatClientError
from .models import ActionPlan, ExecutionResult, Intent, VerificationResult


_VERIFIER_SYSTEM_PROMPT = (
    "You are the execution verifier for the Ainux operating system.\n"
    "Given an intent, the current plan, and execution history, determine whether"
    " the user's request has been satisfied.\n\n"
    "Respond as JSON using the schema:\n"
    "{\n"
    "  \"satisfied\": bool,\n"
    "  \"confidence\": number (0-1),\n"
    "  \"reason\": string\n"
    "}\n"
    "If the outcome is not satisfied, explain what remains or what to adjust."
)


@dataclass
class ResultVerifier:
    """Determines whether executed actions achieved the user's goal."""

    client: Optional[ChatClient] = None

    def verify(
        self,
        intent: Intent,
        plan: ActionPlan,
        history: List[ExecutionResult],
        context: Optional[Dict[str, object]] = None,
    ) -> VerificationResult:
        context = context or {}
        if self.client:
            try:
                return self._verify_with_model(intent, plan, history, context)
            except (ChatClientError, ValueError, json.JSONDecodeError):
                pass
        return self._heuristic_verify(history)

    def _verify_with_model(
        self,
        intent: Intent,
        plan: ActionPlan,
        history: List[ExecutionResult],
        context: Dict[str, object],
    ) -> VerificationResult:
        payload = {
            "intent": {
                "action": intent.action,
                "parameters": intent.parameters,
                "confidence": intent.confidence,
                "raw_input": intent.raw_input,
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
            {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        completion = self.client.create_chat_completion(
            messages,
            response_format={"type": "json_object"},
            extra_options={"seed": 6},
        )
        data = json.loads(completion.content)
        satisfied = bool(data.get("satisfied"))
        confidence_raw = data.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 1.0 if satisfied else 0.0
        reason = data.get("reason") or data.get("message") or data.get("notes")
        return VerificationResult(
            satisfied=satisfied,
            confidence=max(0.0, min(1.0, confidence)),
            reasoning=str(reason) if reason else None,
        )

    def _heuristic_verify(self, history: List[ExecutionResult]) -> VerificationResult:
        if not history:
            return VerificationResult(
                satisfied=False,
                confidence=0.0,
                reasoning="No execution steps have run yet.",
            )

        failed = [
            result
            for result in history
            if result.status in {"error", "blocked"} or result.error
        ]
        if failed:
            last = failed[-1]
            reason = last.error or last.output or last.status
            return VerificationResult(
                satisfied=False,
                confidence=0.1,
                reasoning=reason,
            )

        last = history[-1]
        reason = last.output or last.status
        return VerificationResult(satisfied=True, confidence=0.8, reasoning=reason)


__all__ = ["ResultVerifier"]
