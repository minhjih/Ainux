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
    "Users describe high level goals involving resource management, process"
    " control, user assistance, or general system automation. Read the request"
    " and produce a concise JSON object describing the normalized intent.\n\n"
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
        action = str(payload.get("action") or "analysis.review_request")
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
        action = "analysis.review_request"
        parameters: Dict[str, object] = {}
        confidence = 0.4

        pointer_keywords = ["마우스", "mouse", "커서", "포인터"]
        terminal_keywords = [
            "terminal",
            "터미널",
            "콘솔",
            "console",
            "shell",
            "쉘",
            "bash",
            "zsh",
        ]
        resource_keywords = ["cpu", "메모리", "memory", "ram", "자원", "resource", "load", "부하"]
        process_keywords = ["프로세스", "process", "작업", "kill", "종료", "pid", "백그라운드"]
        ui_keywords = ["도와", "ui", "interface", "창", "앱", "app", "실행", "어떻게", "사용법"]
        schedule_keywords = ["스케줄", "예약", "schedule", "maint", "cron", "시간", "알람"]

        if any(keyword in lowered for keyword in pointer_keywords):
            action = "ui.control_pointer"
            parameters = self._infer_pointer_parameters(request, lowered)
            confidence = 0.8
        elif any(keyword in lowered for keyword in terminal_keywords):
            action = "system.launch_application"
            parameters = {"target": "terminal", "original_request": request}
            confidence = 0.75
        elif any(keyword in lowered for keyword in resource_keywords):
            action = "system.optimize_resources"
            confidence = 0.7
        elif any(keyword in lowered for keyword in process_keywords):
            action = "process.manage"
            confidence = 0.7
        elif any(keyword in lowered for keyword in ui_keywords):
            action = "ui.assist_user"
            confidence = 0.65
        elif any(keyword in lowered for keyword in schedule_keywords):
            action = "system.schedule_task"
            confidence = 0.6
        elif any(keyword in lowered for keyword in ["업데이트", "update", "upgrade"]):
            action = "system.update"
            confidence = 0.5
        elif "network" in lowered or "방화벽" in lowered or "네트워크" in lowered:
            action = "system.schedule_task"
            confidence = 0.4

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

    def _infer_pointer_parameters(self, request: str, lowered: str) -> Dict[str, object]:
        parameters: Dict[str, object] = {}

        operation = "click" if ("클릭" in lowered or "click" in lowered) else "move"
        parameters["operation"] = operation

        if operation == "click":
            button = "left"
            if any(keyword in lowered for keyword in ["오른쪽", "right"]):
                button = "right"
            elif any(keyword in lowered for keyword in ["가운데", "middle"]):
                button = "middle"
            parameters["button"] = button
            if "더블" in lowered or "double" in lowered:
                parameters["clicks"] = 2
            if "길게" in lowered or "hold" in lowered:
                parameters["interval"] = 0.4
            return parameters

        amount = 80
        if any(token in lowered for token in ["조금", "살짝", "약간"]):
            amount = 40
        elif any(token in lowered for token in ["많이", "크게", "멀리"]):
            amount = 140

        match = re.search(r"(\d+)\s*(?:px|픽셀|pixel)?", lowered)
        if match:
            try:
                amount = max(int(match.group(1)), 1)
            except ValueError:
                pass

        dx = 0
        dy = 0
        direction_map = {
            "오른쪽": (1, 0),
            "right": (1, 0),
            "왼쪽": (-1, 0),
            "left": (-1, 0),
            "위": (0, -1),
            "위쪽": (0, -1),
            "up": (0, -1),
            "아래": (0, 1),
            "아랫": (0, 1),
            "down": (0, 1),
        }
        for keyword, vector in direction_map.items():
            if keyword in lowered:
                dx += vector[0]
                dy += vector[1]

        if dx == 0 and dy == 0:
            dx = 1

        parameters["dx"] = dx * amount
        parameters["dy"] = dy * amount

        if any(token in lowered for token in ["천천히", "slow"]):
            parameters["duration"] = 0.4
        elif any(token in lowered for token in ["빠르게", "빨리", "fast"]):
            parameters["duration"] = 0.0

        return parameters
