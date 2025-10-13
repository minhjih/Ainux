"""Event bus utilities for the context fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, MutableMapping, Optional


@dataclass
class ContextEvent:
    """Represents a recorded event in the context fabric."""

    event_type: str
    payload: Dict[str, object]
    timestamp: datetime
    related_nodes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": self.event_type,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
            "related_nodes": list(self.related_nodes),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "ContextEvent":
        event_type = str(payload.get("type", ""))
        if not event_type:
            raise ValueError("Event type is required")
        raw_timestamp = payload.get("timestamp")
        if isinstance(raw_timestamp, str):
            timestamp = datetime.fromisoformat(raw_timestamp)
        else:
            timestamp = datetime.now(timezone.utc)
        related = payload.get("related_nodes")
        if isinstance(related, list):
            related_nodes = [str(item) for item in related]
        else:
            related_nodes = []
        payload_meta = payload.get("payload")
        payload_data = dict(payload_meta) if isinstance(payload_meta, dict) else {}
        return cls(event_type=event_type, payload=payload_data, timestamp=timestamp, related_nodes=related_nodes)


class EventBus:
    """Synchronous event bus with bounded history."""

    def __init__(self, *, max_history: int = 500) -> None:
        self._subscribers: MutableMapping[str, List[Callable[[ContextEvent], None]]] = {}
        self._history: List[ContextEvent] = []
        self._max_history = max_history

    def subscribe(self, event_type: str, callback: Callable[[ContextEvent], None]) -> None:
        if not event_type:
            raise ValueError("event_type must be provided")
        self._subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: str, callback: Callable[[ContextEvent], None]) -> None:
        callbacks = self._subscribers.get(event_type)
        if not callbacks:
            return
        try:
            callbacks.remove(callback)
        except ValueError:
            return

    def emit(
        self,
        event_type: str,
        payload: Optional[Dict[str, object]] = None,
        *,
        related_nodes: Optional[Iterable[str]] = None,
        timestamp: Optional[datetime] = None,
    ) -> ContextEvent:
        """Publish an event and notify subscribers."""

        if not event_type:
            raise ValueError("event_type must be provided")
        event = ContextEvent(
            event_type=event_type,
            payload=dict(payload or {}),
            timestamp=timestamp or datetime.now(timezone.utc),
            related_nodes=list(related_nodes or []),
        )
        self._history.append(event)
        if len(self._history) > self._max_history:
            overflow = len(self._history) - self._max_history
            if overflow > 0:
                del self._history[:overflow]
        for callback in self._subscribers.get(event_type, []):
            callback(event)
        for callback in self._subscribers.get("*", []):
            callback(event)
        return event

    def history(self, *, event_type: Optional[str] = None, limit: Optional[int] = None) -> List[ContextEvent]:
        events = self._history
        if event_type:
            events = [event for event in events if event.event_type == event_type]
        if limit is not None and limit >= 0:
            return list(events[-limit:])
        return list(events)

    def to_dict(self) -> Dict[str, object]:
        return {"events": [event.to_dict() for event in self._history], "max_history": self._max_history}

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "EventBus":
        max_history = int(payload.get("max_history", 500))
        bus = cls(max_history=max_history)
        for item in payload.get("events", []):
            if not isinstance(item, dict):
                continue
            try:
                event = ContextEvent.from_dict(item)
            except ValueError:
                continue
            bus.emit(event.event_type, event.payload, related_nodes=event.related_nodes, timestamp=event.timestamp)
        return bus


__all__ = ["ContextEvent", "EventBus"]
