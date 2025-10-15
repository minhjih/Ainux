"""Dependency graph utilities for hardware automation."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set


class DependencyCycleError(RuntimeError):
    """Raised when dependency resolution finds a cycle."""


@dataclass
class DependencyNode:
    """Node representing a package, driver, or firmware artifact."""

    name: str
    kind: str
    metadata: Dict[str, object]


class DependencyGraph:
    """Directed acyclic graph describing install/update ordering."""

    def __init__(self) -> None:
        self._nodes: Dict[str, DependencyNode] = {}
        self._edges: Dict[str, Set[str]] = defaultdict(set)
        self._reverse_edges: Dict[str, Set[str]] = defaultdict(set)

    def add_node(self, name: str, kind: str, **metadata: object) -> None:
        if name not in self._nodes:
            self._nodes[name] = DependencyNode(name=name, kind=kind, metadata=metadata)

    def add_dependency(self, item: str, depends_on: str) -> None:
        if item == depends_on:
            return
        self._edges[item].add(depends_on)
        self._reverse_edges[depends_on].add(item)

    def remove_node(self, name: str) -> None:
        self._nodes.pop(name, None)
        for deps in self._edges.values():
            deps.discard(name)
        for deps in self._reverse_edges.values():
            deps.discard(name)
        self._edges.pop(name, None)
        self._reverse_edges.pop(name, None)

    def nodes(self) -> Iterable[DependencyNode]:
        return list(self._nodes.values())

    def dependencies_of(self, name: str) -> Set[str]:
        return set(self._edges.get(name, set()))

    def dependents_of(self, name: str) -> Set[str]:
        return set(self._reverse_edges.get(name, set()))

    def topological_sort(self, selected: Optional[Iterable[str]] = None) -> List[DependencyNode]:
        """Return nodes in dependency order, optionally limited to a subset."""

        if selected is None:
            selected_set = set(self._nodes.keys())
        else:
            selected_set = set(selected)

        in_degree: Dict[str, int] = {}
        for name in selected_set:
            deps = self._edges.get(name, set()) & selected_set
            in_degree[name] = len(deps)

        queue = deque([name for name, degree in in_degree.items() if degree == 0])
        ordered: List[DependencyNode] = []

        while queue:
            name = queue.popleft()
            ordered.append(self._nodes[name])
            for dependent in self._reverse_edges.get(name, set()):
                if dependent not in selected_set:
                    continue
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(ordered) != len(selected_set):
            missing = selected_set - {node.name for node in ordered}
            raise DependencyCycleError(
                f"Dependency cycle detected among: {', '.join(sorted(missing))}"
            )

        return ordered

    def to_install_plan(self, selected: Optional[Iterable[str]] = None) -> List[Dict[str, object]]:
        plan: List[Dict[str, object]] = []
        for node in self.topological_sort(selected):
            plan.append(
                {
                    "name": node.name,
                    "kind": node.kind,
                    "metadata": node.metadata,
                }
            )
        return plan
