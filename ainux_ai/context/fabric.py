"""Context fabric implementation combining a knowledge graph and event bus."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Union

from ..config import ensure_config_dir
from .events import ContextEvent, EventBus
from .graph import KnowledgeGraph

FABRIC_PATH_ENV = "AINUX_CONTEXT_FABRIC_PATH"
ROOT_NODE_ID = "context:root"


@dataclass
class ContextSnapshot:
    """Serializable snapshot of the context fabric."""

    graph: KnowledgeGraph
    events: List[ContextEvent] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "graph": self.graph.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "metadata": self.metadata,
        }

    def to_context_payload(self) -> Dict[str, object]:
        """Return a JSON-serializable payload suitable for orchestrator context."""

        return {
            "nodes": self.graph.to_dict().get("nodes", []),
            "edges": self.graph.to_dict().get("edges", []),
            "events": [event.to_dict() for event in self.events],
            "metadata": self.metadata,
        }


class ContextFabric:
    """Maintains a knowledge graph plus event history for orchestration context."""

    def __init__(
        self,
        *,
        graph: Optional[KnowledgeGraph] = None,
        event_bus: Optional[EventBus] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> None:
        self.graph = graph or KnowledgeGraph()
        self.event_bus = event_bus or EventBus()
        self.metadata = dict(metadata or {})
        self._ensure_root()

    def _ensure_root(self) -> None:
        if self.graph.get_node(ROOT_NODE_ID) is None:
            self.graph.upsert_node(
                ROOT_NODE_ID,
                "workspace",
                {
                    "label": "Context fabric root",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    def ingest_file(
        self,
        path: Union[str, Path],
        *,
        label: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        compute_hash: bool = False,
    ) -> str:
        """Record or update a file node and emit an event."""

        file_path = Path(path).expanduser().resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        stat = file_path.stat()
        attributes: Dict[str, object] = {
            "path": str(file_path),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "permissions": oct(stat.st_mode & 0o777),
        }
        if label:
            attributes["label"] = label
        if tags:
            unique_tags = sorted({tag.strip() for tag in tags if tag.strip()})
            if unique_tags:
                attributes["tags"] = unique_tags
        if compute_hash:
            digest = sha256()
            with file_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            attributes["sha256"] = digest.hexdigest()

        node_id = f"file:{file_path}"
        self.graph.upsert_node(node_id, "file", attributes)
        self.graph.add_edge(ROOT_NODE_ID, node_id, "contains")
        event_payload = {
            "path": str(file_path),
            "label": label,
            "tags": attributes.get("tags", []),
        }
        self.event_bus.emit("fabric.file.updated", event_payload, related_nodes=[node_id])
        return node_id

    def ingest_setting(
        self,
        key: str,
        value: object,
        *,
        scope: str = "system",
        metadata: Optional[Dict[str, object]] = None,
    ) -> str:
        """Track a configuration setting in the graph."""

        if not key:
            raise ValueError("key must be provided")
        node_id = f"setting:{scope}:{key}"
        attributes = {
            "key": key,
            "scope": scope,
            "value": value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            attributes["metadata"] = metadata
        self.graph.upsert_node(node_id, "setting", attributes)
        self.graph.add_edge(ROOT_NODE_ID, node_id, "has_setting")
        event_payload = {"key": key, "scope": scope, "value": value}
        self.event_bus.emit("fabric.setting.updated", event_payload, related_nodes=[node_id])
        return node_id

    def record_event(
        self,
        event_type: str,
        payload: Optional[Dict[str, object]] = None,
        *,
        related_nodes: Optional[Iterable[str]] = None,
    ) -> ContextEvent:
        """Record an event in the bus and materialize it in the graph."""

        event = self.event_bus.emit(event_type, payload or {}, related_nodes=related_nodes or [])
        event_node_id = f"event:{event.timestamp.isoformat()}"
        self.graph.upsert_node(
            event_node_id,
            "event",
            {
                "event_type": event.event_type,
                "payload": event.payload,
                "timestamp": event.timestamp.isoformat(),
            },
        )
        self.graph.add_edge(ROOT_NODE_ID, event_node_id, "contains_event")
        for related in event.related_nodes:
            self.graph.add_edge(event_node_id, related, "relates_to")
        return event

    def link_nodes(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        attributes: Optional[Dict[str, object]] = None,
    ) -> None:
        """Create a relationship between existing nodes."""

        if self.graph.get_node(source) is None:
            raise ValueError(f"Unknown node: {source}")
        if self.graph.get_node(target) is None:
            raise ValueError(f"Unknown node: {target}")
        self.graph.add_edge(source, target, relation, attributes)
        self.record_event(
            "fabric.edge.created",
            {"source": source, "target": target, "relation": relation},
            related_nodes=[source, target],
        )

    def merge_metadata(self, payload: Dict[str, object]) -> None:
        for key, value in payload.items():
            self.metadata[key] = value

    def snapshot(self, *, event_limit: int = 50) -> ContextSnapshot:
        events = self.event_bus.history(limit=event_limit)
        metadata = dict(self.metadata)
        metadata.setdefault("node_count", len(list(self.graph.nodes())))
        metadata.setdefault("edge_count", len(list(self.graph.edges())))
        metadata.setdefault("event_count", len(self.event_bus.history()))
        return ContextSnapshot(graph=self.graph, events=events, metadata=metadata)

    def to_dict(self) -> Dict[str, object]:
        return {
            "graph": self.graph.to_dict(),
            "events": self.event_bus.to_dict(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "ContextFabric":
        graph_meta = payload.get("graph")
        if isinstance(graph_meta, dict):
            graph = KnowledgeGraph.from_dict(graph_meta)
        else:
            graph = KnowledgeGraph()
        events_meta = payload.get("events")
        if isinstance(events_meta, dict):
            event_bus = EventBus.from_dict(events_meta)
        else:
            event_bus = EventBus()
        metadata = dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {}
        return cls(graph=graph, event_bus=event_bus, metadata=metadata)

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        target = Path(path).expanduser() if path else default_fabric_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        tmp_path = target.with_suffix(".tmp")
        tmp_path.write_text(serialized, encoding="utf-8")
        os.replace(tmp_path, target)
        try:
            os.chmod(target, 0o600)
        except PermissionError:
            pass
        return target


def default_fabric_path() -> Path:
    override = os.environ.get(FABRIC_PATH_ENV)
    if override:
        return Path(override).expanduser()
    config_path = ensure_config_dir()
    return config_path.parent / "context_fabric.json"


def load_fabric(path: Optional[Union[str, Path]] = None) -> ContextFabric:
    target = Path(path).expanduser() if path else default_fabric_path()
    if not target.exists():
        return ContextFabric()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ContextFabric()
    fabric = ContextFabric.from_dict(payload)
    return fabric


def save_fabric(fabric: ContextFabric, path: Optional[Union[str, Path]] = None) -> Path:
    return fabric.save(path)


__all__ = [
    "ContextFabric",
    "ContextSnapshot",
    "default_fabric_path",
    "load_fabric",
    "save_fabric",
]
