"""Intent parsing for natural language orchestration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Optional

from ..client import ChatClient, ChatClientError
from .models import Intent


_INTENT_SYSTEM_PROMPT = (
    "You are the intent parser for the Ainux operating system.\n"
    "Users describe high level goals involving hardware automation, scheduling,"
    " or system management. Read the request and produce a concise JSON object"
    " describing the normalized intent.\n\n"
    "Respond using the following JSON schema:\n"
    "{\n"
    "  \"action\": string // short snake_case identifier describing the goal\n"
    "  \"confidence\": number // between 0 and 1\n"
    "  \"parameters\": object // optional parameters inferred from the request\n"
    "  \"reasoning\": string // one sentence summary explaining the decision\n"
    "}\n\n"
    "If you are unsure, choose the closest action and lower the confidence."
)


@dataclass
class IntentParser:
    """Convert natural language requests into structured intents."""

    client: Optional[ChatClient] = None

    def parse(self, request: str, context: Optional[Dict[str, object]] = None) -> Intent:
        """Return a structured :class:`Intent` for *request*."""

        request = request.strip()
        if not request:
            raise ValueError("Request must not be empty")

        context_snapshot = context or {}

        if self.client:
            try:
                return self._parse_with_model(request, context_snapshot)
            except (ChatClientError, ValueError, json.JSONDecodeError):
                # Fall back to heuristics if the model call fails or returns invalid JSON.
                pass
        return self._heuristic_parse(request, context_snapshot)

    def _parse_with_model(self, request: str, context: Dict[str, object]) -> Intent:
        messages = [
            {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps({"request": request, "context": context}, ensure_ascii=False),
            },
        ]
        completion = self.client.create_chat_completion(
            messages,
            response_format={"type": "json_object"},
            extra_options={"seed": 1},
        )
        payload = json.loads(completion.content)
        action = str(payload.get("action") or "free_form")
        confidence = float(payload.get("confidence") or 0.0)
        reasoning = payload.get("reasoning")
        parameters = payload.get("parameters") or {}
        if not isinstance(parameters, dict):
            parameters = {"value": parameters}
        return Intent(
            raw_input=request,
            action=action,
            parameters=parameters,
            confidence=confidence,
            reasoning=str(reasoning) if reasoning is not None else None,
            context_snapshot=context,
        )

    def _heuristic_parse(self, request: str, context: Dict[str, object]) -> Intent:
        lowered = request.lower()
        action = "free_form"
        parameters: Dict[str, object] = {}
        confidence = 0.4

        if any(keyword in lowered for keyword in ["cuda", "gpu", "드라이버"]):
            action = "hardware.provision_gpu_stack"
            confidence = 0.7
        elif any(keyword in lowered for keyword in ["스케줄", "예약", "schedule", "maint"]):
            action = "orchestration.schedule_maintenance"
            confidence = 0.6
        elif "패킷" in lowered or "network" in lowered or "방화벽" in lowered:
            action = "network.configure"
            confidence = 0.6
        elif any(keyword in lowered for keyword in ["업데이트", "update", "upgrade"]):
            action = "system.update"
            confidence = 0.5

        window_match = re.search(r"(\d{1,2})(?:시|:)(\d{2})?", request)
        if window_match:
            hour = window_match.group(1)
            minute = window_match.group(2) or "00"
            parameters["requested_time"] = f"{hour}:{minute}"
        day_match = re.search(r"(월|화|수|목|금|토|일)요일", request)
        if day_match:
            parameters["requested_day"] = day_match.group(0)
        return Intent(
            raw_input=request,
            action=action,
            parameters=parameters,
            confidence=confidence,
            reasoning="Heuristic parser",
            context_snapshot=context,
        )
