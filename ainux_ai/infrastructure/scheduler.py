"""Infrastructure scheduler helpers for blueprints and job control."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from ..config import ensure_config_dir
from ..context import ContextFabric, load_fabric

BLUEPRINT_ROOT_ENV = "AINUX_BLUEPRINT_ROOT"
DEFAULT_PLAYBOOK_ROOT = Path("/usr/local/share/ainux/playbooks")
WINDOWS_FILENAME = "scheduler_windows.json"


class SchedulerError(RuntimeError):
    """Raised when scheduling workflows fail."""


@dataclass
class BlueprintExecutionResult:
    """Metadata about a blueprint execution attempt."""

    name: str
    path: Path
    command: List[str]
    dry_run: bool
    extra_vars: Dict[str, str] = field(default_factory=dict)
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class JobSubmissionResult:
    """Details about a batch job submission."""

    job_id: str
    command: List[str]
    stdout: str
    stderr: str
    simulated: bool = False
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MaintenanceWindow:
    """Represents a maintenance window tracked by the scheduler."""

    name: str
    start: datetime
    end: datetime
    targets: List[str] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "targets": self.targets,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "MaintenanceWindow":
        return cls(
            name=str(payload.get("name", "")) or "window",
            start=datetime.fromisoformat(str(payload.get("start"))),
            end=datetime.fromisoformat(str(payload.get("end"))),
            targets=list(payload.get("targets", [])),
            metadata=dict(payload.get("metadata", {})),
        )


def default_blueprint_root() -> Path:
    """Return the default path containing automation blueprints."""

    override = os.environ.get(BLUEPRINT_ROOT_ENV)
    if override:
        return Path(override).expanduser()
    if DEFAULT_PLAYBOOK_ROOT.exists():
        return DEFAULT_PLAYBOOK_ROOT
    config_path = ensure_config_dir()
    return config_path.parent / "playbooks"


def default_windows_path() -> Path:
    config_path = ensure_config_dir()
    windows_path = config_path.parent / WINDOWS_FILENAME
    windows_path.parent.mkdir(parents=True, exist_ok=True)
    return windows_path


class SchedulerService:
    """Coordinate blueprint execution, maintenance windows, and batch jobs."""

    def __init__(
        self,
        *,
        blueprint_root: Optional[Path] = None,
        context_fabric: Optional[ContextFabric] = None,
        fabric_path: Optional[Path] = None,
        windows_path: Optional[Path] = None,
    ) -> None:
        self.blueprint_root = Path(blueprint_root or default_blueprint_root()).expanduser()
        self.fabric = context_fabric
        self.fabric_path = fabric_path
        self.windows_path = Path(windows_path or default_windows_path()).expanduser()
        self._windows_cache: Optional[List[MaintenanceWindow]] = None

    # ------------------------------------------------------------------
    # Blueprint helpers
    # ------------------------------------------------------------------
    def list_blueprints(self) -> List[str]:
        """Return available blueprint paths relative to *blueprint_root*."""

        if not self.blueprint_root.exists():
            return []
        results: List[str] = []
        for path in sorted(self.blueprint_root.rglob("*.yml")):
            results.append(str(path.relative_to(self.blueprint_root)))
        for path in sorted(self.blueprint_root.rglob("*.yaml")):
            rel = str(path.relative_to(self.blueprint_root))
            if rel not in results:
                results.append(rel)
        return results

    def run_blueprint(
        self,
        name: str,
        *,
        extra_vars: Optional[Dict[str, str]] = None,
        dry_run: bool = False,
        tags: Optional[Iterable[str]] = None,
    ) -> BlueprintExecutionResult:
        """Execute an Ansible blueprint and return metadata."""

        blueprint_path = self._resolve_blueprint(name)
        extra_vars = dict(extra_vars or {})
        command = ["ansible-playbook", str(blueprint_path)]
        for key, value in extra_vars.items():
            command.extend(["--extra-vars", f"{key}={value}"])
        if tags:
            command.extend(["--tags", ",".join(tags)])
        if dry_run:
            command.append("--check")

        ansible_path = shutil.which("ansible-playbook")
        result = BlueprintExecutionResult(
            name=name,
            path=blueprint_path,
            command=command,
            dry_run=dry_run,
            extra_vars=extra_vars,
        )
        if ansible_path is None:
            if not dry_run:
                raise SchedulerError(
                    "ansible-playbook is not available. Install Ansible or enable --dry-run."
                )
            self._record_event(
                "scheduler.blueprint.simulated",
                {
                    "blueprint": str(blueprint_path),
                    "extra_vars": extra_vars,
                },
            )
            return result

        exec_cmd = [ansible_path, *command[1:]]
        proc = subprocess.run(exec_cmd, capture_output=True, text=True)
        result.stdout = proc.stdout
        result.stderr = proc.stderr
        result.returncode = proc.returncode
        if proc.returncode != 0:
            raise SchedulerError(
                f"Blueprint '{name}' failed with code {proc.returncode}: {proc.stderr.strip()}"
            )
        self._record_event(
            "scheduler.blueprint.executed",
            {
                "blueprint": str(blueprint_path),
                "extra_vars": extra_vars,
                "tags": list(tags or []),
            },
        )
        return result

    # ------------------------------------------------------------------
    # Batch job helpers
    # ------------------------------------------------------------------
    def submit_job(
        self,
        job_args: Sequence[str],
        *,
        dry_run: bool = False,
    ) -> JobSubmissionResult:
        if not job_args:
            raise SchedulerError("At least one argument must be supplied for the scheduler job")

        command = ["sbatch", *job_args]
        sbatch_path = shutil.which("sbatch")
        if sbatch_path is None or dry_run:
            job_id = f"sim-{uuid.uuid4()}"
            self._record_event(
                "scheduler.job.simulated",
                {"args": list(job_args), "job_id": job_id, "dry_run": dry_run or sbatch_path is None},
            )
            return JobSubmissionResult(
                job_id=job_id,
                command=command,
                stdout="",
                stderr="sbatch unavailable" if sbatch_path is None else "",
                simulated=True,
            )

        proc = subprocess.run([sbatch_path, *job_args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SchedulerError(f"sbatch failed: {proc.stderr.strip()}")
        job_id = self._parse_job_id(proc.stdout)
        self._record_event(
            "scheduler.job.submitted",
            {"args": list(job_args), "job_id": job_id},
        )
        return JobSubmissionResult(job_id=job_id, command=command, stdout=proc.stdout, stderr=proc.stderr)

    def job_status(self, status_args: Sequence[str]) -> str:
        command = ["squeue", *status_args]
        squeue_path = shutil.which("squeue")
        if squeue_path is None:
            raise SchedulerError("squeue is not available on this system")
        proc = subprocess.run([squeue_path, *status_args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SchedulerError(f"squeue failed: {proc.stderr.strip()}")
        self._record_event("scheduler.job.status", {"args": list(status_args)})
        return proc.stdout

    def cancel_job(self, job_id: str, extra_args: Sequence[str] = ()) -> None:
        if not job_id:
            raise SchedulerError("job_id must be provided for cancellation")
        command = ["scancel", *extra_args, job_id]
        scancel_path = shutil.which("scancel")
        if scancel_path is None:
            raise SchedulerError("scancel is not available on this system")
        proc = subprocess.run([scancel_path, *extra_args, job_id], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SchedulerError(f"scancel failed: {proc.stderr.strip()}")
        self._record_event(
            "scheduler.job.cancelled",
            {"job_id": job_id, "args": list(extra_args)},
        )

    def collect_targets(self) -> List[str]:
        """Return known targets from the context fabric and windows."""

        targets = set()
        fabric = self._ensure_fabric()
        if fabric is not None:
            for node in fabric.graph.nodes():
                if node.type in {"hardware", "host", "service", "cluster_node"}:
                    hostname = str(node.attributes.get("hostname") or node.attributes.get("name"))
                    identifier = hostname or node.attributes.get("id")
                    if identifier:
                        targets.add(str(identifier))
        for window in self.list_windows():
            for target in window.targets:
                if target:
                    targets.add(target)
        return sorted(targets)

    # ------------------------------------------------------------------
    # Maintenance windows
    # ------------------------------------------------------------------
    def list_windows(self) -> List[MaintenanceWindow]:
        windows = self._load_windows()
        return sorted(windows, key=lambda item: item.start)

    def create_window(
        self,
        name: str,
        *,
        duration_minutes: int,
        targets: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, object]] = None,
    ) -> MaintenanceWindow:
        if duration_minutes <= 0:
            raise SchedulerError("duration_minutes must be positive")
        start = datetime.now(timezone.utc)
        end = start + timedelta(minutes=duration_minutes)
        window = MaintenanceWindow(
            name=name,
            start=start,
            end=end,
            targets=list(targets or []),
            metadata=dict(metadata or {}),
        )
        windows = self._load_windows()
        windows.append(window)
        self._save_windows(windows)
        self._record_event(
            "scheduler.window.created",
            {
                "name": name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "targets": window.targets,
            },
        )
        return window

    def close_window(self, name: str) -> bool:
        windows = self._load_windows()
        remaining: List[MaintenanceWindow] = []
        closed = False
        for window in windows:
            if window.name == name and not closed:
                closed = True
                self._record_event(
                    "scheduler.window.closed",
                    {
                        "name": name,
                        "start": window.start.isoformat(),
                        "end": window.end.isoformat(),
                    },
                )
                continue
            remaining.append(window)
        if closed:
            self._save_windows(remaining)
        return closed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_blueprint(self, name: str) -> Path:
        if not name:
            raise SchedulerError("Blueprint name must be provided")
        candidate = Path(name)
        search_paths = []
        if candidate.is_absolute() and candidate.exists():
            return candidate
        if candidate.exists():
            return candidate
        if candidate.suffix not in {".yml", ".yaml"}:
            search_paths.append(self.blueprint_root / f"{name}.yml")
            search_paths.append(self.blueprint_root / f"{name}.yaml")
        search_paths.append(self.blueprint_root / name)
        for path in search_paths:
            if path.exists():
                return path
        matches = list(self.blueprint_root.rglob(name))
        if matches:
            return matches[0]
        raise SchedulerError(f"Blueprint '{name}' not found under {self.blueprint_root}")

    def _parse_job_id(self, stdout: str) -> str:
        for token in stdout.strip().split():
            if token.isdigit():
                return token
        return stdout.strip() or f"job-{uuid.uuid4()}"

    def _load_windows(self) -> List[MaintenanceWindow]:
        if self._windows_cache is not None:
            return list(self._windows_cache)
        if not self.windows_path.exists():
            self._windows_cache = []
            return []
        try:
            payload = json.loads(self.windows_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SchedulerError(f"Failed to load maintenance windows: {exc}") from exc
        windows: List[MaintenanceWindow] = []
        for item in payload.get("windows", []):
            if isinstance(item, dict):
                try:
                    windows.append(MaintenanceWindow.from_dict(item))
                except Exception:
                    continue
        self._windows_cache = windows
        return list(windows)

    def _save_windows(self, windows: Sequence[MaintenanceWindow]) -> None:
        payload = {"windows": [window.to_dict() for window in windows]}
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        tmp_path = self.windows_path.with_suffix(".tmp")
        tmp_path.write_text(serialized, encoding="utf-8")
        tmp_path.replace(self.windows_path)
        self._windows_cache = list(windows)

    def _ensure_fabric(self) -> Optional[ContextFabric]:
        if self.fabric is None and self.fabric_path:
            self.fabric = load_fabric(self.fabric_path)
        return self.fabric

    def _record_event(self, event_type: str, payload: Dict[str, object]) -> None:
        fabric = self._ensure_fabric()
        if fabric is None:
            return
        fabric.record_event(event_type, payload)
        if self.fabric_path:
            fabric.save(self.fabric_path)

