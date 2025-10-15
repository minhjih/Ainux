"""Knowledge graph primitives for the Ainux context fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class ContextNode:
    """Represents an entity tracked inside the context fabric."""

    id: str
    type: str
    attributes: Dict[str, object] = field(default_factory=dict)

    def merge(self, **attributes: object) -> None:
        """Merge *attributes* into the node."""

        for key, value in attributes.items():
            if value is None:
                continue
            self.attributes[key] = value


@dataclass
class ContextEdge:
    """Relationship between two context nodes."""

    source: str
    target: str
    relation: str
    attributes: Dict[str, object] = field(default_factory=dict)

    def merge(self, **attributes: object) -> None:
        for key, value in attributes.items():
            if value is None:
                continue
            self.attributes[key] = value


class KnowledgeGraph:
    """In-memory knowledge graph for files, settings, and events."""

    def __init__(self) -> None:
        self._nodes: Dict[str, ContextNode] = {}
        self._edges: Dict[Tuple[str, str, str], ContextEdge] = {}

    def upsert_node(
        self, node_id: str, node_type: str, attributes: Optional[Dict[str, object]] = None
    ) -> ContextNode:
        """Insert or update a node."""

        if not node_id:
            raise ValueError("node_id must be provided")
        if not node_type:
            raise ValueError("node_type must be provided")

        node = self._nodes.get(node_id)
        if node is None:
            node = ContextNode(id=node_id, type=node_type, attributes=dict(attributes or {}))
            self._nodes[node_id] = node
        else:
            if node.type != node_type:
                node.type = node_type
            if attributes:
                node.merge(**attributes)
        return node

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        attributes: Optional[Dict[str, object]] = None,
    ) -> ContextEdge:
        """Insert or update an edge."""

        if not source or not target or not relation:
            raise ValueError("source, target, and relation must be provided for an edge")

        key = (source, target, relation)
        edge = self._edges.get(key)
        if edge is None:
            edge = ContextEdge(source=source, target=target, relation=relation, attributes=dict(attributes or {}))
            self._edges[key] = edge
        else:
            if attributes:
                edge.merge(**attributes)
        return edge

    def get_node(self, node_id: str) -> Optional[ContextNode]:
        return self._nodes.get(node_id)

    def neighbors(self, node_id: str, relation: Optional[str] = None) -> List[ContextNode]:
        """Return neighbor nodes for *node_id* optionally filtered by *relation*."""

        neighbors: List[ContextNode] = []
        for (source, target, rel), edge in self._edges.items():
            if relation and rel != relation:
                continue
            if source == node_id and target in self._nodes:
                neighbors.append(self._nodes[target])
            elif target == node_id and source in self._nodes:
                neighbors.append(self._nodes[source])
        return neighbors

    def remove_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        for key in list(self._edges):
            if node_id in key[:2]:
                self._edges.pop(key, None)

    def remove_edge(self, source: str, target: str, relation: str) -> None:
        key = (source, target, relation)
        self._edges.pop(key, None)

    def nodes(self) -> Iterable[ContextNode]:
        return list(self._nodes.values())

    def edges(self) -> Iterable[ContextEdge]:
        return list(self._edges.values())

    def to_dict(self) -> Dict[str, object]:
        return {
            "nodes": [
                {"id": node.id, "type": node.type, "attributes": node.attributes}
                for node in self._nodes.values()
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "relation": edge.relation,
                    "attributes": edge.attributes,
                }
                for edge in self._edges.values()
            ],
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "KnowledgeGraph":
        graph = cls()
        for node_meta in payload.get("nodes", []):
            if not isinstance(node_meta, dict):
                continue
            node_id = str(node_meta.get("id", ""))
            node_type = str(node_meta.get("type", ""))
            attributes = dict(node_meta.get("attributes", {})) if isinstance(node_meta.get("attributes"), dict) else {}
            if node_id and node_type:
                graph.upsert_node(node_id, node_type, attributes)
        for edge_meta in payload.get("edges", []):
            if not isinstance(edge_meta, dict):
                continue
            source = str(edge_meta.get("source", ""))
            target = str(edge_meta.get("target", ""))
            relation = str(edge_meta.get("relation", ""))
            attributes = (
                dict(edge_meta.get("attributes", {})) if isinstance(edge_meta.get("attributes"), dict) else {}
            )
            if source and target and relation:
                graph.add_edge(source, target, relation, attributes)
        return graph


__all__ = ["ContextNode", "ContextEdge", "KnowledgeGraph"]
