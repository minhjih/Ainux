"""Command line interface for the Ainux AI GPT client."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.request import urlopen

from . import __version__
from .context import ContextFabric, default_fabric_path, load_fabric
from .client import ChatClient, ChatClientError, ChatCompletion, format_usage
from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ConfigError,
    ensure_config_dir,
    list_providers,
    load_config,
    mask_secret,
    remove_provider,
    resolve_provider,
    set_default_provider,
    update_provider_api_key,
    upsert_provider,
)
from .hardware import (
    HardwareAutomationError,
    HardwareAutomationService,
    DriverPackage,
    FirmwarePackage,
    TelemetrySample,
    default_catalog_path as default_hardware_catalog_path,
)
from .infrastructure import (
    NetworkAutomationError,
    NetworkAutomationService,
    QoSPolicy,
    SchedulerError,
    SchedulerService,
    ClusterHealthError,
    ClusterHealthService,
    default_blueprint_root,
    default_profiles_path,
    HealthReport,
)
from .orchestration import AinuxOrchestrator, OrchestrationError


DEFAULT_UPSTREAM_REPO = "https://github.com/ainux-os/Ainux.git"
DEFAULT_UPSTREAM_REF = "main"
from .ui import AinuxUIServer, UIServerConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ainux-ai-chat",
        description="Connect Ainux workflows to GPT-style APIs via configurable providers.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    chat_parser = subcommands.add_parser("chat", help="Send a prompt or start an interactive session.")
    chat_parser.add_argument("--provider", help="Provider name from configuration or environment.")
    chat_parser.add_argument("-m", "--message", help="Message to send. Reads stdin if omitted and not interactive.")
    chat_parser.add_argument(
        "-f",
        "--message-file",
        help="Path to a file containing the user message. Overrides --message if provided.",
    )
    chat_parser.add_argument("-s", "--system", help="Optional system prompt to seed the conversation.")
    chat_parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature (default: 0.2).")
    chat_parser.add_argument("--max-tokens", type=int, help="Maximum completion tokens.")
    chat_parser.add_argument(
        "--response-format",
        help="JSON encoded response_format payload or shorthand 'json'/'text'.",
    )
    chat_parser.add_argument(
        "--extra-option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional options to merge into the chat request (VALUE parsed as JSON when possible).",
    )
    chat_parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds (default: 60).")
    chat_parser.add_argument("--json", action="store_true", help="Emit raw JSON response instead of plain text.")
    chat_parser.add_argument(
        "--interactive",
        action="store_true",
        help="Enter a multi-turn interactive session (default when no message is supplied and stdin is a TTY).",
    )
    chat_parser.add_argument(
        "--history",
        help="Append JSONL conversation transcripts to this path (created if missing).",
    )
    chat_parser.set_defaults(func=handle_chat)

    configure_parser = subcommands.add_parser(
        "configure", help="Create or update an API provider configuration entry."
    )
    configure_parser.add_argument("name", nargs="?", help="Provider name (default: openai).")
    configure_parser.add_argument("--api-key", help="API key value. Prompted securely when omitted.")
    configure_parser.add_argument("--model", help="Model identifier (default: gpt-4o-mini).")
    configure_parser.add_argument("--base-url", help="Base API URL (default: https://api.openai.com/v1).")
    configure_parser.add_argument("--organization", help="Optional OpenAI organization header.")
    configure_parser.add_argument(
        "--extra-header",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional HTTP headers for the provider (repeat for multiple).",
    )
    configure_parser.add_argument(
        "--default", action="store_true", help="Mark this provider as the default after saving."
    )
    configure_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting when required values are missing.",
    )
    configure_parser.set_defaults(func=handle_configure)

    list_parser = subcommands.add_parser("providers", help="List configured providers.")
    list_parser.add_argument("--json", action="store_true", help="Return provider metadata as JSON.")
    list_parser.add_argument(
        "--show-keys",
        action="store_true",
        help="Display full API keys (defaults to masked). Use with caution.",
    )
    list_parser.set_defaults(func=handle_list_providers)

    remove_parser = subcommands.add_parser("remove", help="Delete a provider configuration entry.")
    remove_parser.add_argument("name", help="Provider name to remove.")
    remove_parser.set_defaults(func=handle_remove)

    default_parser = subcommands.add_parser("set-default", help="Set the default provider for chat requests.")
    default_parser.add_argument("name", help="Provider name to mark as default.")
    default_parser.set_defaults(func=handle_set_default)

    set_key_parser = subcommands.add_parser(
        "set-key",
        help="Quickly update the API key for a provider without re-entering other settings.",
    )
    set_key_parser.add_argument(
        "name",
        nargs="?",
        help="Provider name to update (defaults to the configured default or 'openai').",
    )
    set_key_parser.add_argument("--api-key", help="API key value. Prompted securely when omitted.")
    set_key_parser.add_argument(
        "--base-url",
        help="Override base URL when creating or updating (defaults preserved if omitted).",
    )
    set_key_parser.add_argument(
        "--model",
        help="Override model identifier when creating or updating (defaults preserved if omitted).",
    )
    set_key_parser.add_argument(
        "--organization",
        help="Override OpenAI organization header (set to empty string to clear).",
    )
    set_key_parser.add_argument(
        "--create",
        action="store_true",
        help="Create the provider if it does not already exist.",
    )
    set_key_parser.add_argument(
        "--make-default",
        action="store_true",
        help="Mark the provider as default after updating the key.",
    )
    set_key_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting when required values are missing.",
    )
    set_key_parser.set_defaults(func=handle_set_key)

    assist_parser = subcommands.add_parser(
        "assist",
        help="Ask Ainux to handle an OS task from natural language input.",
    )
    assist_parser.add_argument(
        "request",
        nargs="?",
        help="Natural language request. Reads stdin when omitted.",
    )
    assist_parser.add_argument(
        "--provider",
        help="Provider name for AI-assisted planning (falls back to heuristics when missing).",
    )
    assist_parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds (default: 60).",
    )
    assist_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the plan without executing commands.",
    )
    assist_parser.add_argument(
        "--offline",
        action="store_true",
        help="Use heuristic planning even when a provider is configured.",
    )
    assist_parser.add_argument(
        "--no-context",
        action="store_true",
        help="Skip loading and updating the shared context fabric.",
    )
    assist_parser.add_argument(
        "--fabric-path",
        help="Override the path used to load/save the context fabric.",
    )
    assist_parser.set_defaults(func=handle_assist)

    self_update_parser = subcommands.add_parser(
        "self-update",
        help="Update the installed ainux-ai tools from the upstream Git repository.",
    )
    self_update_parser.add_argument(
        "--repo-url",
        default=DEFAULT_UPSTREAM_REPO,
        help=(
            "Git repository URL to pull updates from (default:"
            f" {DEFAULT_UPSTREAM_REPO})."
        ),
    )
    self_update_parser.add_argument(
        "--ref",
        default=DEFAULT_UPSTREAM_REF,
        help=(
            "Branch, tag, or commit to check out from the upstream repository"
            f" (default: {DEFAULT_UPSTREAM_REF})."
        ),
    )
    self_update_parser.add_argument(
        "--tarball-url",
        help=(
            "Fallback tarball URL to download when git is unavailable."
            " Defaults to the GitHub codeload URL derived from --repo-url."
        ),
    )
    self_update_parser.add_argument(
        "--install-root",
        help=(
            "Root directory that contains the ainux_ai package to replace."
            " Defaults to the directory two levels above this CLI module."
        ),
    )
    self_update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the update steps without downloading or modifying files.",
    )
    self_update_parser.set_defaults(func=handle_self_update)

    orchestrate_parser = subcommands.add_parser(
        "orchestrate",
        help="Run the natural-language orchestrator to produce plans and optional execution logs.",
    )
    orchestrate_parser.add_argument(
        "request",
        nargs="?",
        help="Natural language request. Reads stdin when omitted.",
    )
    orchestrate_parser.add_argument(
        "--provider",
        help="Provider name for AI-assisted parsing and planning (falls back to heuristics when missing).",
    )
    orchestrate_parser.add_argument(
        "--context",
        help="Path to a JSON file providing additional context for the planner.",
    )
    orchestrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip capability execution and return plan only.",
    )
    orchestrate_parser.add_argument(
        "--offline",
        action="store_true",
        help="Force heuristic mode without contacting a GPT provider.",
    )
    orchestrate_parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout for GPT calls (default: 60).",
    )
    orchestrate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit orchestration results as JSON.",
    )
    orchestrate_parser.add_argument(
        "--use-fabric",
        action="store_true",
        help="Merge the context fabric snapshot into planning context and log the request.",
    )
    orchestrate_parser.add_argument(
        "--fabric-path",
        help="Override the context fabric state path (default: ~/.config/ainux/context_fabric.json).",
    )
    orchestrate_parser.add_argument(
        "--fabric-event-limit",
        type=int,
        default=50,
        help="Number of recent events to include when using the context fabric (default: 50).",
    )
    orchestrate_parser.set_defaults(func=handle_orchestrate)

    ui_parser = subcommands.add_parser(
        "ui",
        help="Launch the browser-based orchestration studio.",
    )
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1).")
    ui_parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="HTTP port to listen on (default: 8787).",
    )
    ui_parser.add_argument("--provider", help="Preferred provider name for GPT requests.")
    ui_parser.add_argument(
        "--offline",
        action="store_true",
        help="Start in offline heuristic mode without contacting a GPT provider.",
    )
    ui_parser.add_argument(
        "--execute",
        action="store_true",
        help="Allow real command execution (default: dry-run only).",
    )
    ui_parser.add_argument(
        "--no-fabric",
        action="store_true",
        help="Disable context fabric integration.",
    )
    ui_parser.add_argument(
        "--fabric-path",
        help="Override the path for context fabric persistence.",
    )
    ui_parser.add_argument(
        "--fabric-event-limit",
        type=int,
        default=20,
        help="Number of fabric events to surface in the UI (default: 20).",
    )
    ui_parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout for GPT calls in seconds (default: 60).",
    )
    ui_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the default web browser.",
    )
    ui_parser.set_defaults(func=handle_ui)

    context_parser = subcommands.add_parser(
        "context",
        help="Inspect and update the Ainux context fabric knowledge graph.",
    )
    context_parser.add_argument(
        "--path",
        help="Path to the context fabric state file (default: ~/.config/ainux/context_fabric.json).",
    )
    context_subcommands = context_parser.add_subparsers(dest="context_command")
    context_subcommands.required = True

    snapshot_parser = context_subcommands.add_parser(
        "snapshot",
        help="Display a snapshot of the context fabric.",
    )
    snapshot_parser.add_argument(
        "--limit-events",
        type=int,
        default=20,
        help="Number of recent events to include (default: 20).",
    )
    snapshot_parser.add_argument("--json", action="store_true", help="Emit the snapshot as JSON.")
    snapshot_parser.add_argument(
        "--output",
        help="Write the snapshot JSON to a file path.",
    )
    snapshot_parser.set_defaults(func=handle_fabric_snapshot)

    ingest_file_parser = context_subcommands.add_parser(
        "ingest-file",
        help="Record file metadata into the context fabric.",
    )
    ingest_file_parser.add_argument("file", help="File path to record.")
    ingest_file_parser.add_argument("--label", help="Friendly label for the file node.")
    ingest_file_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="Tag to associate with the file (repeatable).",
    )
    ingest_file_parser.add_argument(
        "--hash",
        action="store_true",
        help="Compute a SHA256 checksum for the file contents.",
    )
    ingest_file_parser.set_defaults(func=handle_fabric_ingest_file)

    ingest_setting_parser = context_subcommands.add_parser(
        "ingest-setting",
        help="Track a configuration setting inside the fabric.",
    )
    ingest_setting_parser.add_argument("key", help="Setting key.")
    ingest_setting_parser.add_argument("value", help="Setting value (JSON parsed when possible).")
    ingest_setting_parser.add_argument(
        "--scope",
        default="system",
        help="Scope label for the setting (default: system).",
    )
    ingest_setting_parser.add_argument(
        "--metadata",
        help="JSON metadata object to attach to the setting.",
    )
    ingest_setting_parser.set_defaults(func=handle_fabric_ingest_setting)

    record_event_parser = context_subcommands.add_parser(
        "record-event",
        help="Append an event to the context fabric history.",
    )
    record_event_parser.add_argument("event_type", help="Event type identifier.")
    record_event_parser.add_argument(
        "--data",
        help="JSON payload describing the event.",
    )
    record_event_parser.add_argument(
        "--related",
        action="append",
        default=[],
        metavar="NODE_ID",
        help="Node identifier related to the event (repeatable).",
    )
    record_event_parser.set_defaults(func=handle_fabric_record_event)

    link_parser = context_subcommands.add_parser(
        "link",
        help="Create a relationship between existing nodes.",
    )
    link_parser.add_argument("source", help="Source node identifier.")
    link_parser.add_argument("target", help="Target node identifier.")
    link_parser.add_argument("relation", help="Relation name.")
    link_parser.add_argument(
        "--attributes",
        help="JSON object describing edge attributes.",
    )
    link_parser.set_defaults(func=handle_fabric_link)

    clear_parser = context_subcommands.add_parser(
        "clear",
        help="Reset the context fabric state file.",
    )
    clear_parser.add_argument(
        "--preserve-metadata",
        action="store_true",
        help="Keep existing metadata when clearing nodes and events.",
    )
    clear_parser.set_defaults(func=handle_fabric_clear)

    hardware_parser = subcommands.add_parser(
        "hardware",
        help="자동 하드웨어 카탈로그, 텔레메트리, 실행 계획을 관리합니다.",
    )
    hardware_parser.add_argument(
        "--catalog-path",
        help=(
            "카탈로그 파일 경로를 재정의합니다 (기본값: "
            f"{default_hardware_catalog_path()})."
        ),
    )
    hardware_parser.add_argument(
        "--no-fabric",
        action="store_true",
        help="컨텍스트 패브릭 로깅을 비활성화합니다.",
    )
    hardware_parser.add_argument(
        "--fabric-path",
        help="컨텍스트 패브릭 파일 경로를 재정의합니다.",
    )
    hardware_sub = hardware_parser.add_subparsers(dest="hardware_command")
    hardware_sub.required = True

    hw_scan = hardware_sub.add_parser(
        "scan",
        help="시스템 하드웨어 인벤토리를 스캔하고 카탈로그에 병합합니다.",
    )
    hw_scan.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    hw_scan.add_argument(
        "--no-persist",
        action="store_true",
        help="스캔 결과를 카탈로그에 저장하지 않습니다.",
    )
    hw_scan.set_defaults(func=handle_hardware_scan)

    hw_catalog = hardware_sub.add_parser(
        "catalog",
        help="카탈로그 내용을 조회하거나 블루프린트를 등록합니다.",
    )
    catalog_sub = hw_catalog.add_subparsers(dest="hardware_catalog_command")
    catalog_sub.required = True

    hw_catalog_show = catalog_sub.add_parser(
        "show",
        help="전체 카탈로그 요약을 확인합니다.",
    )
    hw_catalog_show.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    hw_catalog_show.set_defaults(func=handle_hardware_catalog_show)

    hw_catalog_drivers = catalog_sub.add_parser(
        "drivers",
        help="등록된 드라이버 블루프린트를 나열합니다.",
    )
    hw_catalog_drivers.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    hw_catalog_drivers.set_defaults(func=handle_hardware_catalog_drivers)

    hw_catalog_firmware = catalog_sub.add_parser(
        "firmware",
        help="등록된 펌웨어 블루프린트를 나열합니다.",
    )
    hw_catalog_firmware.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    hw_catalog_firmware.set_defaults(func=handle_hardware_catalog_firmware)

    hw_catalog_blueprints = catalog_sub.add_parser(
        "blueprints",
        help="사전 정의된 하드웨어 자동화 블루프린트를 확인합니다.",
    )
    hw_catalog_blueprints.add_argument(
        "--json", action="store_true", help="JSON 형식으로 출력합니다."
    )
    hw_catalog_blueprints.set_defaults(func=handle_hardware_catalog_blueprints)

    hw_add_driver = catalog_sub.add_parser(
        "add-driver",
        help="새 드라이버 블루프린트를 카탈로그에 추가합니다.",
    )
    hw_add_driver.add_argument("name", help="드라이버 이름")
    hw_add_driver.add_argument("version", help="드라이버 버전")
    hw_add_driver.add_argument(
        "--package",
        action="append",
        required=True,
        dest="packages",
        help="설치할 패키지 이름 (반복 가능)",
    )
    hw_add_driver.add_argument(
        "--module",
        action="append",
        default=[],
        dest="modules",
        help="필요한 커널 모듈 이름",
    )
    hw_add_driver.add_argument(
        "--vendor",
        help="제조사 식별자",
    )
    hw_add_driver.add_argument(
        "--supports",
        action="append",
        default=[],
        dest="supports",
        help="지원하는 하드웨어 ID (반복 가능)",
    )
    hw_add_driver.add_argument(
        "--requires",
        action="append",
        default=[],
        dest="requires",
        help="설치 전 필요한 항목",
    )
    hw_add_driver.add_argument(
        "--provides",
        action="append",
        default=[],
        dest="provides",
        help="설치 후 제공하는 가상 기능",
    )
    hw_add_driver.set_defaults(func=handle_hardware_add_driver)

    hw_add_firmware = catalog_sub.add_parser(
        "add-firmware",
        help="새 펌웨어 블루프린트를 카탈로그에 추가합니다.",
    )
    hw_add_firmware.add_argument("name", help="펌웨어 이름")
    hw_add_firmware.add_argument("version", help="펌웨어 버전")
    hw_add_firmware.add_argument(
        "--file",
        action="append",
        required=True,
        dest="files",
        help="복사할 펌웨어 파일 경로 (반복 가능)",
    )
    hw_add_firmware.add_argument(
        "--vendor",
        help="제조사 식별자",
    )
    hw_add_firmware.add_argument(
        "--supports",
        action="append",
        default=[],
        dest="supports",
        help="지원하는 하드웨어 ID (반복 가능)",
    )
    hw_add_firmware.add_argument(
        "--requires",
        action="append",
        default=[],
        dest="requires",
        help="설치 전 필요한 항목",
    )
    hw_add_firmware.set_defaults(func=handle_hardware_add_firmware)

    hw_plan = hardware_sub.add_parser(
        "plan",
        help="하드웨어 드라이버/펌웨어 설치 계획을 생성합니다.",
    )
    hw_plan.add_argument(
        "--component",
        action="append",
        default=[],
        dest="components",
        help="특정 컴포넌트 ID만 대상으로 지정합니다.",
    )
    hw_plan.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    hw_plan.add_argument(
        "--apply",
        action="store_true",
        help="생성된 계획을 즉시 실행합니다.",
    )
    hw_plan.add_argument(
        "--dry-run",
        action="store_true",
        help="실행 명령을 출력만 하고 실제로 실행하지 않습니다 (--apply와 함께 사용).",
    )
    hw_plan.set_defaults(func=handle_hardware_plan)

    hw_telemetry = hardware_sub.add_parser(
        "telemetry",
        help="시스템 텔레메트리 스냅샷을 수집합니다.",
    )
    hw_telemetry.add_argument(
        "--samples",
        type=int,
        default=1,
        help="수집할 샘플 개수 (기본값 1).",
    )
    hw_telemetry.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="샘플 사이 간격(초).")
    hw_telemetry.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    hw_telemetry.set_defaults(func=handle_hardware_telemetry)

    scheduler_parser = subcommands.add_parser(
        "scheduler",
        help="정비 블루프린트와 배치 스케줄러를 제어합니다.",
    )
    scheduler_parser.add_argument(
        "--blueprint-root",
        help=(
            "블루프린트 디렉터리를 지정합니다 (기본값: "
            f"{default_blueprint_root()})."
        ),
    )
    scheduler_parser.add_argument(
        "--no-fabric",
        action="store_true",
        help="컨텍스트 패브릭 이벤트 기록을 비활성화합니다.",
    )
    scheduler_parser.add_argument(
        "--fabric-path",
        help="컨텍스트 패브릭 파일 경로를 재정의합니다.",
    )
    scheduler_sub = scheduler_parser.add_subparsers(dest="scheduler_command")
    scheduler_sub.required = True

    scheduler_list = scheduler_sub.add_parser(
        "list",
        help="사용 가능한 블루프린트를 나열합니다.",
    )
    scheduler_list.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    scheduler_list.set_defaults(func=handle_scheduler_list)

    scheduler_run = scheduler_sub.add_parser(
        "run",
        help="블루프린트를 ansible-playbook으로 실행합니다.",
    )
    scheduler_run.add_argument("name", help="실행할 블루프린트 이름 또는 경로")
    scheduler_run.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="추가 Ansible 변수 (반복 가능)",
    )
    scheduler_run.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        help="지정한 태그만 실행합니다.",
    )
    scheduler_run.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 실행 대신 --check 모드로 시뮬레이션합니다.",
    )
    scheduler_run.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    scheduler_run.set_defaults(func=handle_scheduler_run)

    scheduler_job = scheduler_sub.add_parser(
        "job",
        help="sbatch를 통해 배치 작업을 제출합니다.",
    )
    scheduler_job.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="sbatch에 전달할 인수",
    )
    scheduler_job.add_argument(
        "--dry-run",
        action="store_true",
        help="sbatch가 없거나 테스트 용도로 제출을 시뮬레이션합니다.",
    )
    scheduler_job.set_defaults(func=handle_scheduler_job)

    scheduler_status = scheduler_sub.add_parser(
        "status",
        help="squeue를 사용하여 현재 큐를 확인합니다.",
    )
    scheduler_status.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="squeue에 전달할 인수",
    )
    scheduler_status.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    scheduler_status.set_defaults(func=handle_scheduler_status)

    scheduler_cancel = scheduler_sub.add_parser(
        "cancel",
        help="scancel을 사용하여 작업을 취소합니다.",
    )
    scheduler_cancel.add_argument("job_id", help="취소할 작업 ID")
    scheduler_cancel.add_argument(
        "extra",
        nargs="*",
        help="추가 scancel 인수",
    )
    scheduler_cancel.set_defaults(func=handle_scheduler_cancel)

    scheduler_targets = scheduler_sub.add_parser(
        "targets",
        help="알려진 스케줄링 대상 목록을 출력합니다.",
    )
    scheduler_targets.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    scheduler_targets.set_defaults(func=handle_scheduler_targets)

    scheduler_window = scheduler_sub.add_parser(
        "window",
        help="정비 윈도우를 생성/조회합니다.",
    )
    window_sub = scheduler_window.add_subparsers(dest="scheduler_window_command")
    window_sub.required = True

    scheduler_window_create = window_sub.add_parser(
        "create",
        help="새로운 정비 윈도우를 생성합니다.",
    )
    scheduler_window_create.add_argument("name", help="윈도우 이름")
    scheduler_window_create.add_argument(
        "--duration",
        type=int,
        default=60,
        help="윈도우 지속 시간(분)",
    )
    scheduler_window_create.add_argument(
        "--target",
        action="append",
        default=[],
        dest="targets",
        help="적용할 대상 식별자 (반복 가능)",
    )
    scheduler_window_create.add_argument(
        "--meta",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="추가 메타데이터",
    )
    scheduler_window_create.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    scheduler_window_create.set_defaults(func=handle_scheduler_window_create)

    scheduler_window_list = window_sub.add_parser(
        "list",
        help="등록된 정비 윈도우를 나열합니다.",
    )
    scheduler_window_list.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    scheduler_window_list.set_defaults(func=handle_scheduler_window_list)

    scheduler_window_close = window_sub.add_parser(
        "close",
        help="지정한 정비 윈도우를 종료합니다.",
    )
    scheduler_window_close.add_argument("name", help="종료할 윈도우 이름")
    scheduler_window_close.set_defaults(func=handle_scheduler_window_close)

    network_parser = subcommands.add_parser(
        "network",
        help="네트워크 프로파일과 QoS 정책을 조율합니다.",
    )
    network_parser.add_argument(
        "--profiles-path",
        help=(
            "프로파일 저장소 경로를 지정합니다 (기본값: "
            f"{default_profiles_path()})."
        ),
    )
    network_parser.add_argument(
        "--no-fabric",
        action="store_true",
        help="컨텍스트 패브릭 기록을 비활성화합니다.",
    )
    network_parser.add_argument(
        "--fabric-path",
        help="컨텍스트 패브릭 파일 경로를 재정의합니다.",
    )
    network_sub = network_parser.add_subparsers(dest="network_command")
    network_sub.required = True

    network_list = network_sub.add_parser(
        "list",
        help="등록된 네트워크 프로파일을 나열합니다.",
    )
    network_list.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    network_list.set_defaults(func=handle_network_list)

    network_save = network_sub.add_parser(
        "save",
        help="새 네트워크 프로파일을 저장하거나 갱신합니다.",
    )
    network_save.add_argument("name", help="프로파일 이름")
    network_save.add_argument(
        "--interface",
        action="append",
        default=[],
        dest="interfaces",
        help="프로파일에 포함될 인터페이스 (반복 가능)",
    )
    network_save.add_argument(
        "--vlan",
        action="append",
        default=[],
        help="VLAN 정의 (parent:id[:address])",
    )
    network_save.add_argument(
        "--qos",
        action="append",
        default=[],
        help="QoS 정책 정의 (iface:rate[:burst])",
    )
    network_save.add_argument(
        "--firewall",
        action="append",
        default=[],
        help="nftables 규칙 라인 (반복 가능)",
    )
    network_save.add_argument(
        "--description",
        help="프로파일 설명",
    )
    network_save.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="추가 메타데이터",
    )
    network_save.set_defaults(func=handle_network_save)

    network_apply = network_sub.add_parser(
        "apply",
        help="네트워크 프로파일을 적용합니다.",
    )
    network_apply.add_argument("name", help="적용할 프로파일 이름")
    network_apply.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 명령을 실행하지 않고 계획만 출력합니다.",
    )
    network_apply.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    network_apply.set_defaults(func=handle_network_apply)

    network_delete = network_sub.add_parser(
        "delete",
        help="프로파일을 삭제합니다.",
    )
    network_delete.add_argument("name", help="삭제할 프로파일 이름")
    network_delete.set_defaults(func=handle_network_delete)

    network_snapshot = network_sub.add_parser(
        "snapshot",
        help="현재 네트워크 인터페이스 상태를 스냅샷합니다.",
    )
    network_snapshot.set_defaults(func=handle_network_snapshot)

    network_qos = network_sub.add_parser(
        "qos",
        help="단일 QoS 정책을 즉시 적용합니다.",
    )
    network_qos.add_argument("definition", help="정의 형식: iface:rate[:burst]")
    network_qos.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 적용 대신 명령만 출력합니다.",
    )
    network_qos.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    network_qos.set_defaults(func=handle_network_qos)

    cluster_parser = subcommands.add_parser(
        "cluster",
        help="클러스터 헬스 텔레메트리를 수집합니다.",
    )
    cluster_parser.add_argument(
        "--no-fabric",
        action="store_true",
        help="컨텍스트 패브릭 기록을 비활성화합니다.",
    )
    cluster_parser.add_argument(
        "--fabric-path",
        help="컨텍스트 패브릭 파일 경로를 재정의합니다.",
    )
    cluster_sub = cluster_parser.add_subparsers(dest="cluster_command")
    cluster_sub.required = True

    cluster_snapshot = cluster_sub.add_parser(
        "snapshot",
        help="현재 시스템 상태를 한 번 수집합니다.",
    )
    cluster_snapshot.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    cluster_snapshot.set_defaults(func=handle_cluster_snapshot)

    cluster_watch = cluster_sub.add_parser(
        "watch",
        help="지정된 간격으로 반복 수집합니다.",
    )
    cluster_watch.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="샘플 간격(초)",
    )
    cluster_watch.add_argument(
        "--limit",
        type=int,
        help="수집할 최대 횟수",
    )
    cluster_watch.add_argument("--json", action="store_true", help="JSON 형식으로 출력합니다.")
    cluster_watch.set_defaults(func=handle_cluster_watch)

    return parser


def handle_chat(args: argparse.Namespace) -> int:
    interactive = args.interactive or (
        args.message is None and args.message_file is None and sys.stdin.isatty()
    )

    try:
        provider = resolve_provider(args.provider)
    except ConfigError as exc:
        raise ConfigError(str(exc))

    response_format = _parse_response_format(args.response_format)
    extra_options = _parse_extra_options(args.extra_option)

    client = ChatClient(provider, timeout=args.timeout)
    base_messages: List[Dict[str, object]] = []
    if args.system:
        base_messages.append({"role": "system", "content": args.system})

    if interactive:
        return _interactive_loop(client, base_messages, args, response_format, extra_options)

    if args.message_file and args.message:
        raise ConfigError("Provide either --message or --message-file, not both.")

    if args.message_file:
        message = Path(args.message_file).read_text(encoding="utf-8")
    elif args.message is not None:
        message = args.message
    else:
        if sys.stdin.isatty():
            message = input("Prompt: ")
        else:
            message = sys.stdin.read()

    messages = list(base_messages)
    messages.append({"role": "user", "content": message})

    completion = client.create_chat_completion(
        messages,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        response_format=response_format,
        extra_options=extra_options,
    )
    _emit_completion(completion, args)
    if args.history:
        history_messages = list(messages)
        history_messages.append({"role": "assistant", "content": completion.content})
        _append_history(args.history, provider.name, history_messages, completion)
    return 0


def handle_configure(args: argparse.Namespace) -> int:
    ensure_config_dir()
    name = args.name or _prompt_default("Provider name", "openai", args.non_interactive)
    model = args.model or _prompt_default("Model", DEFAULT_MODEL, args.non_interactive)
    base_url = args.base_url or _prompt_default("Base URL", DEFAULT_BASE_URL, args.non_interactive)
    api_key = args.api_key or _prompt_secret("API key", args.non_interactive)
    organization = args.organization or _prompt_optional("Organization", args.non_interactive)
    extra_headers = _collect_extra_headers(args.extra_header)

    saved_path = upsert_provider(
        name=name,
        api_key=api_key,
        base_url=base_url,
        model=model,
        organization=organization,
        extra_headers=extra_headers,
        make_default=args.default,
    )

    print(f"Saved provider '{name}' to {saved_path}")
    if args.default:
        print(f"'{name}' marked as default provider.")
    return 0


def handle_list_providers(args: argparse.Namespace) -> int:
    providers = list_providers()
    default_name = load_config().get("default_provider")
    if args.json:
        payload = {
            "default_provider": default_name,
            "providers": [
                {
                    "name": provider.name,
                    "base_url": provider.base_url,
                    "model": provider.model,
                    "organization": provider.organization,
                    "extra_headers": provider.extra_headers,
                    "api_key": provider.api_key if args.show_keys else mask_secret(provider.api_key),
                }
                for provider in providers
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if not providers:
        print("No providers configured. Run 'ainux-ai-chat configure' first.")
        return 0

    print("Providers ( * marks default ): \n")
    print(f"{'*':1} {'Name':15} {'Model':18} Base URL")
    print("-" * 72)
    for provider in providers:
        marker = "*" if provider.name == default_name else " "
        line = f"{marker} {provider.name:15} {provider.model:18} {provider.base_url}"
        print(line)
        if provider.organization:
            print(f"    org: {provider.organization}")
        key_display = provider.api_key if args.show_keys else mask_secret(provider.api_key)
        print(f"    key: {key_display}")
        if provider.extra_headers:
            print(f"    headers: {provider.extra_headers}")
    return 0


def handle_remove(args: argparse.Namespace) -> int:
    remove_provider(args.name)
    print(f"Removed provider '{args.name}'.")
    return 0


def handle_set_default(args: argparse.Namespace) -> int:
    set_default_provider(args.name)
    print(f"Provider '{args.name}' set as default.")
    return 0


def handle_set_key(args: argparse.Namespace) -> int:
    config = load_config()
    provider_name = args.name or config.get("default_provider") or "openai"
    providers = config.get("providers", {})
    provider_exists = provider_name in providers

    allow_create = args.create or (not provider_exists and not providers and provider_name == "openai")

    if args.non_interactive and not args.api_key:
        raise ConfigError("--api-key must be supplied when running non-interactively")

    api_key = args.api_key or _prompt_secret("API key", args.non_interactive)
    base_url = args.base_url
    model = args.model
    organization = args.organization

    saved_path, resolved_name = update_provider_api_key(
        provider_name,
        api_key,
        base_url=base_url,
        model=model,
        organization=organization,
        create_missing=allow_create,
        make_default=args.make_default,
    )

    verb = "Created" if not provider_exists and allow_create else "Updated"
    print(f"{verb} provider '{resolved_name}' in {saved_path}.")
    if args.make_default:
        print(f"'{resolved_name}' marked as default provider.")
    return 0


def derive_tarball_url(repo_url: str, ref: str) -> Optional[str]:
    if not repo_url:
        return None
    normalized = repo_url.rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    github_prefix = "https://github.com/"
    if normalized.startswith(github_prefix):
        repo_path = normalized[len(github_prefix) :]
        return f"https://codeload.github.com/{repo_path}/tar.gz/{ref}" if ref else f"https://codeload.github.com/{repo_path}/tar.gz/main"
    return None


def find_repo_root(extracted_dir: Path) -> Optional[Path]:
    if not extracted_dir.exists():
        return None
    if (extracted_dir / "ainux_ai").is_dir():
        return extracted_dir
    for child in extracted_dir.iterdir():
        if child.is_dir() and (child / "ainux_ai").is_dir():
            return child
    return None


def handle_self_update(args: argparse.Namespace) -> int:
    repo_url = args.repo_url or DEFAULT_UPSTREAM_REPO
    ref = args.ref or DEFAULT_UPSTREAM_REF
    install_root = Path(args.install_root).expanduser() if args.install_root else Path(__file__).resolve().parent.parent

    if not install_root.exists():
        print(f"Install root {install_root} does not exist.", file=sys.stderr)
        return 1

    target_package = install_root / "ainux_ai"
    if not target_package.exists():
        print(
            f"No 'ainux_ai' package found under {install_root}. Use --install-root to point to the correct directory.",
            file=sys.stderr,
        )
        return 1

    print(f"[info] Updating Ainux tooling in {install_root} from {repo_url}@{ref}")

    if args.dry_run:
        print("[dry-run] Skipping download and install because --dry-run was supplied.")
        return 0

    temp_dir = Path(tempfile.mkdtemp(prefix="ainux-self-update-"))
    checkout_dir: Optional[Path] = None

    try:
        git_binary = shutil.which("git")
        if git_binary:
            checkout_dir = temp_dir / "checkout"
            command = [git_binary, "clone", "--depth", "1"]
            if ref:
                command.extend(["--branch", ref])
            command.extend([repo_url, str(checkout_dir)])
            print(f"[info] Cloning repository via: {shlex.join(command)}")
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                print(
                    f"[warn] git clone exited with status {result.returncode}; falling back to tarball download.",
                    file=sys.stderr,
                )
                checkout_dir = None
        else:
            print("[info] git binary not found; attempting tarball download instead.")

        if checkout_dir is None:
            tarball_url = args.tarball_url
            if not tarball_url:
                tarball_url = derive_tarball_url(repo_url, ref)
            if not tarball_url:
                print(
                    "Unable to derive a tarball URL. Provide one explicitly via --tarball-url.",
                    file=sys.stderr,
                )
                return 1

            archive_path = temp_dir / "source.tar.gz"
            print(f"[info] Downloading tarball from {tarball_url}")
            try:
                with urlopen(tarball_url) as response, archive_path.open("wb") as archive_file:
                    shutil.copyfileobj(response, archive_file)
            except URLError as exc:
                print(f"Failed to download update tarball: {exc}", file=sys.stderr)
                return 1

            extracted_dir = temp_dir / "extracted"
            extracted_dir.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(archive_path, "r:gz") as archive:
                    archive.extractall(path=extracted_dir)
            except (tarfile.TarError, OSError) as exc:
                print(f"Failed to extract update tarball: {exc}", file=sys.stderr)
                return 1

            checkout_dir = find_repo_root(extracted_dir)
            if checkout_dir is None:
                print(
                    "Unable to locate the Ainux repository root inside the downloaded tarball.",
                    file=sys.stderr,
                )
                return 1

        source_package = checkout_dir / "ainux_ai"
        if not source_package.is_dir():
            print(
                f"The downloaded repository does not contain an 'ainux_ai' directory at {source_package}",
                file=sys.stderr,
            )
            return 1

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        stage_dir = install_root / f".ainux_ai.new.{timestamp}"
        backup_dir = install_root / f"ainux_ai.backup.{timestamp}"

        print(f"[info] Staging updated package at {stage_dir}")
        try:
            shutil.copytree(source_package, stage_dir)
        except OSError as exc:
            print(f"Failed to stage new package contents: {exc}", file=sys.stderr)
            return 1

        try:
            if target_package.exists():
                print(f"[info] Creating backup of current package at {backup_dir}")
                target_package.rename(backup_dir)

            print(f"[info] Activating updated package at {target_package}")
            stage_dir.rename(target_package)
        except OSError as exc:
            print(f"Failed to activate updated package: {exc}", file=sys.stderr)
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            if backup_dir.exists() and not target_package.exists():
                backup_dir.rename(target_package)
            return 1

        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

        print("Ainux AI tooling is now up to date.")
        return 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def handle_assist(args: argparse.Namespace) -> int:
    request = args.request
    if not request:
        if sys.stdin.isatty():
            request = input("무엇을 도와드릴까요? ")
        else:
            request = sys.stdin.read()
    if not request or not request.strip():
        raise ConfigError("No request supplied for assist command")
    request = request.strip()

    fabric: Optional[ContextFabric] = None
    fabric_path: Optional[Path] = None
    if not args.no_context:
        fabric, fabric_path = _load_context_fabric(args.fabric_path)

    client = None
    provider_warning: Optional[str] = None
    if not args.offline:
        try:
            provider = resolve_provider(args.provider)
        except ConfigError as exc:
            provider_warning = str(exc)
        else:
            client = ChatClient(provider, timeout=args.timeout)

    orchestrator = AinuxOrchestrator.with_client(client, fabric=fabric)

    try:
        result = orchestrator.orchestrate(request, context={}, execute=not args.dry_run)
    except OrchestrationError as exc:
        print(f"Orchestration failed: {exc}", file=sys.stderr)
        return 1

    _print_assist_summary(result, executed=not args.dry_run)

    if result.safety.blocked_steps:
        print("[info] 일부 단계는 안전 검토에서 차단되었습니다.", file=sys.stderr)

    if provider_warning:
        print(f"[info] {provider_warning}. 휴리스틱 모드로 진행했습니다.", file=sys.stderr)

    if fabric is not None and not args.no_context:
        saved_path = fabric.save(fabric_path)
        print(f"[info] Context fabric updated: {saved_path}", file=sys.stderr)

    return 0


def handle_orchestrate(args: argparse.Namespace) -> int:
    context: Dict[str, object] = {}
    if args.context:
        context_path = Path(args.context).expanduser()
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"Context file not found: {context_path}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Failed to parse context JSON: {exc}")

    request = args.request
    if not request:
        if sys.stdin.isatty():
            request = input("Intent: ")
        else:
            request = sys.stdin.read()
    if not request or not request.strip():
        raise ConfigError("No request supplied for orchestration")
    request = request.strip()

    fabric: Optional[ContextFabric] = None
    fabric_path: Optional[Path] = None
    if args.use_fabric or args.fabric_path:
        fabric, fabric_path = _load_context_fabric(args.fabric_path)

    client = None
    if not args.offline:
        try:
            provider = resolve_provider(args.provider)
        except ConfigError as exc:
            print(f"[warn] {exc}. Falling back to heuristic orchestrator.", file=sys.stderr)
        else:
            client = ChatClient(provider, timeout=args.timeout)

    orchestrator = AinuxOrchestrator.with_client(
        client,
        fabric=fabric,
        fabric_event_limit=args.fabric_event_limit,
    )

    try:
        result = orchestrator.orchestrate(request, context=context, execute=not args.dry_run)
    except OrchestrationError as exc:
        print(f"Orchestration failed: {exc}", file=sys.stderr)
        return 1

    payload = _orchestration_result_to_dict(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_orchestration_result(payload)

    if result.safety.blocked_steps:
        print("[info] Some steps were blocked by safety checks.", file=sys.stderr)
    if fabric is not None:
        saved_path = fabric.save(fabric_path)
        print(f"[info] Context fabric updated: {saved_path}", file=sys.stderr)
    return 0


def handle_ui(args: argparse.Namespace) -> int:
    use_fabric = not args.no_fabric
    config = UIServerConfig(
        host=args.host,
        port=args.port,
        provider=args.provider,
        offline=args.offline,
        execute=args.execute,
        use_fabric=use_fabric,
        fabric_path=args.fabric_path,
        fabric_event_limit=args.fabric_event_limit,
        timeout=args.timeout,
    )

    server = AinuxUIServer(config)
    url = server.url
    print(f"Launching Ainux orchestration studio → {url}")
    if not args.execute:
        print("[info] Running in dry-run mode. Pass --execute to enable real command execution.", file=sys.stderr)
    if args.offline:
        print("[info] Offline heuristic mode enabled. Configure a provider to use GPT planning.", file=sys.stderr)
    if args.no_fabric:
        print("[info] Context fabric integration disabled (enable by omitting --no-fabric).", file=sys.stderr)

    try:
        server.serve(open_browser=not args.no_browser)
    except OSError as exc:
        print(f"Failed to start UI server: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[info] UI server stopped.")
    return 0


def _resolve_fabric_path(path: Optional[str]) -> Path:
    if path:
        return Path(path).expanduser()
    return default_fabric_path()


def _load_context_fabric(path: Optional[str]) -> Tuple[ContextFabric, Path]:
    resolved = _resolve_fabric_path(path)
    fabric = load_fabric(resolved)
    return fabric, resolved


def _hardware_service_from_args(args: argparse.Namespace) -> HardwareAutomationService:
    catalog_path = Path(args.catalog_path).expanduser() if getattr(args, "catalog_path", None) else None
    fabric = None
    fabric_path = None
    if not getattr(args, "no_fabric", False):
        fabric, fabric_path = _load_context_fabric(getattr(args, "fabric_path", None))
    service = HardwareAutomationService(
        catalog_path=catalog_path,
        context_fabric=fabric,
        fabric_path=fabric_path,
    )
    return service


def _scheduler_service_from_args(args: argparse.Namespace) -> SchedulerService:
    blueprint_root = Path(args.blueprint_root).expanduser() if getattr(args, "blueprint_root", None) else None
    fabric = None
    fabric_path = None
    if not getattr(args, "no_fabric", False):
        fabric, fabric_path = _load_context_fabric(getattr(args, "fabric_path", None))
    service = SchedulerService(
        blueprint_root=blueprint_root,
        context_fabric=fabric,
        fabric_path=fabric_path,
    )
    return service


def _network_service_from_args(args: argparse.Namespace) -> NetworkAutomationService:
    profiles_path = Path(args.profiles_path).expanduser() if getattr(args, "profiles_path", None) else None
    fabric = None
    fabric_path = None
    if not getattr(args, "no_fabric", False):
        fabric, fabric_path = _load_context_fabric(getattr(args, "fabric_path", None))
    service = NetworkAutomationService(
        profiles_path=profiles_path,
        context_fabric=fabric,
        fabric_path=fabric_path,
    )
    return service


def _cluster_service_from_args(args: argparse.Namespace) -> ClusterHealthService:
    fabric = None
    fabric_path = None
    if not getattr(args, "no_fabric", False):
        fabric, fabric_path = _load_context_fabric(getattr(args, "fabric_path", None))
    service = ClusterHealthService(context_fabric=fabric, fabric_path=fabric_path)
    return service


def _parse_json_arg(raw: Optional[str]) -> object:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def handle_fabric_snapshot(args: argparse.Namespace) -> int:
    fabric, path = _load_context_fabric(args.path)
    snapshot = fabric.snapshot(event_limit=args.limit_events)
    payload = snapshot.to_dict()

    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    metadata = snapshot.metadata
    node_count = metadata.get("node_count", len(list(fabric.graph.nodes())))
    edge_count = metadata.get("edge_count", len(list(fabric.graph.edges())))
    event_count = metadata.get("event_count", len(snapshot.events))

    print(f"Fabric state: {path}")
    print(f"Nodes: {node_count}  Edges: {edge_count}  Events: {event_count}")
    if snapshot.events:
        print("Recent events:")
        for event in snapshot.events:
            related = f" related={event.related_nodes}" if event.related_nodes else ""
            print(f"- {event.timestamp.isoformat()} {event.event_type}{related}")
            if event.payload:
                print(f"    payload: {event.payload}")
    else:
        print("No events recorded yet.")
    return 0


def handle_fabric_ingest_file(args: argparse.Namespace) -> int:
    fabric, path = _load_context_fabric(args.path)
    try:
        node_id = fabric.ingest_file(
            args.file,
            label=args.label,
            tags=args.tag,
            compute_hash=args.hash,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    saved = fabric.save(path)
    print(f"Recorded file '{args.file}' as node '{node_id}'. Saved to {saved}.")
    return 0


def handle_fabric_ingest_setting(args: argparse.Namespace) -> int:
    fabric, path = _load_context_fabric(args.path)
    value = _parse_json_arg(args.value)
    metadata = _parse_json_arg(args.metadata)
    if metadata is not None and not isinstance(metadata, dict):
        print("Metadata must be provided as a JSON object.", file=sys.stderr)
        return 1
    try:
        node_id = fabric.ingest_setting(
            args.key,
            value,
            scope=args.scope,
            metadata=metadata if isinstance(metadata, dict) else None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    saved = fabric.save(path)
    print(f"Setting '{args.key}' recorded as node '{node_id}'. Saved to {saved}.")
    return 0


def handle_fabric_record_event(args: argparse.Namespace) -> int:
    fabric, path = _load_context_fabric(args.path)
    payload = _parse_json_arg(args.data)
    if payload is not None and not isinstance(payload, dict):
        print("Event payload must be a JSON object.", file=sys.stderr)
        return 1
    event = fabric.record_event(
        args.event_type,
        payload if isinstance(payload, dict) else None,
        related_nodes=args.related,
    )
    saved = fabric.save(path)
    print(
        f"Recorded event '{event.event_type}' at {event.timestamp.isoformat()}. Saved to {saved}."
    )
    return 0


def handle_fabric_link(args: argparse.Namespace) -> int:
    fabric, path = _load_context_fabric(args.path)
    attributes = _parse_json_arg(args.attributes)
    if attributes is not None and not isinstance(attributes, dict):
        print("Edge attributes must be a JSON object.", file=sys.stderr)
        return 1
    try:
        fabric.link_nodes(
            args.source,
            args.target,
            args.relation,
            attributes=attributes if isinstance(attributes, dict) else None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    saved = fabric.save(path)
    print(f"Linked {args.source} -[{args.relation}]-> {args.target}. Saved to {saved}.")
    return 0


def handle_fabric_clear(args: argparse.Namespace) -> int:
    fabric, path = _load_context_fabric(args.path)
    metadata = dict(fabric.metadata) if args.preserve_metadata else {}
    new_fabric = ContextFabric(metadata=metadata or None)
    new_fabric.record_event(
        "fabric.reset",
        {
            "preserve_metadata": args.preserve_metadata,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    saved = new_fabric.save(path)
    print(
        f"Cleared context fabric at {saved} (metadata preserved: {args.preserve_metadata})."
    )
    return 0


def handle_hardware_scan(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    components = service.refresh_inventory(persist=not args.no_persist)
    if args.json:
        payload = [asdict(component) for component in components]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"감지된 컴포넌트 {len(components)}개:")
        for component in components:
            tags = f" tags={','.join(component.tags)}" if component.tags else ""
            vendor = component.vendor or "unknown"
            print(
                f" - [{component.category}] {component.identifier}: {component.name}"
                f" (vendor={vendor}{tags})"
            )
        print(f"카탈로그 저장 경로: {service.catalog_path}")
    return 0


def handle_hardware_catalog_show(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    catalog = service.catalog
    if args.json:
        print(json.dumps(catalog.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("카탈로그 요약")
        print(f" - 컴포넌트: {len(catalog.components)}개")
        print(f" - 드라이버: {len(catalog.drivers)}개")
        print(f" - 펌웨어: {len(catalog.firmware)}개")
        print(f" - 블루프린트 키: {', '.join(sorted(catalog.list_blueprints().keys()))}")
    return 0


def handle_hardware_catalog_drivers(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    catalog = service.catalog
    drivers = list(catalog.drivers.values())
    if args.json:
        print(json.dumps([asdict(driver) for driver in drivers], indent=2, ensure_ascii=False))
    else:
        if not drivers:
            print("등록된 드라이버 블루프린트가 없습니다.")
        for driver in drivers:
            modules = f" modules={','.join(driver.kernel_modules)}" if driver.kernel_modules else ""
            supports = f" supports={','.join(driver.supported_ids)}" if driver.supported_ids else ""
            print(
                f" - {driver.name} v{driver.version}: packages={','.join(driver.packages)}"
                f"{modules}{supports}"
            )
    return 0


def handle_hardware_catalog_firmware(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    catalog = service.catalog
    firmware = list(catalog.firmware.values())
    if args.json:
        print(json.dumps([asdict(item) for item in firmware], indent=2, ensure_ascii=False))
    else:
        if not firmware:
            print("등록된 펌웨어 블루프린트가 없습니다.")
        for item in firmware:
            supports = f" supports={','.join(item.supported_ids)}" if item.supported_ids else ""
            print(
                f" - {item.name} v{item.version}: files={','.join(item.files)}{supports}"
            )
    return 0


def handle_hardware_catalog_blueprints(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    blueprints = service.catalog.list_blueprints()
    if args.json:
        print(json.dumps(blueprints, indent=2, ensure_ascii=False))
    else:
        print("블루프린트 목록")
        for key, meta in blueprints.items():
            description = meta.get("description", "")
            packages = ",".join(meta.get("packages", []))
            print(f" - {key}: {description} (packages={packages})")
    return 0


def handle_hardware_add_driver(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    driver = DriverPackage(
        name=args.name,
        version=args.version,
        packages=args.packages,
        kernel_modules=args.modules,
        vendor=args.vendor,
        supported_ids=args.supports,
        requires=args.requires,
        provides=args.provides,
    )
    service.add_driver_blueprint(driver)
    print(f"드라이버 '{driver.name}'(v{driver.version})가 카탈로그에 저장되었습니다.")
    return 0


def handle_hardware_add_firmware(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    firmware = FirmwarePackage(
        name=args.name,
        version=args.version,
        files=args.files,
        vendor=args.vendor,
        supported_ids=args.supports,
        requires=args.requires,
    )
    service.add_firmware_blueprint(firmware)
    print(f"펌웨어 '{firmware.name}'(v{firmware.version})가 카탈로그에 저장되었습니다.")
    return 0


def handle_hardware_plan(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    if args.components:
        missing = [cid for cid in args.components if cid not in service.catalog.components]
        if missing:
            print(f"카탈로그에서 찾을 수 없는 컴포넌트: {', '.join(missing)}", file=sys.stderr)
            return 1
        components = [service.catalog.components[cid] for cid in args.components]
    else:
        components = None

    plan = service.recommend(components)
    plan_payload = {
        "components": [asdict(component) for component in plan.components],
        "drivers": [asdict(driver) for driver in plan.drivers],
        "firmware": [asdict(item) for item in plan.firmware],
        "install_plan": plan.install_plan,
    }

    if args.json:
        print(json.dumps(plan_payload, indent=2, ensure_ascii=False))
    else:
        print(f"대상 컴포넌트 {len(plan.components)}개")
        for component in plan.components:
            print(f" - {component.identifier}: {component.name}")
        print(f"추천 드라이버 {len(plan.drivers)}개, 펌웨어 {len(plan.firmware)}개")
        if plan.install_plan:
            print("실행 단계:")
            for step in plan.install_plan:
                name = step.get("name")
                kind = step.get("kind")
                print(f" - [{kind}] {name}")
        else:
            print("실행할 단계가 없습니다.")

    commands: List[str] = []
    if args.apply:
        dry_run = args.dry_run or False
        commands = service.execute_plan(plan.install_plan, dry_run=dry_run)
        if dry_run:
            print("드라이런 모드: 다음 명령이 실행 대상입니다:")
            for command in commands:
                print(f"   $ {command}")
        else:
            print("계획이 성공적으로 실행되었습니다.")
    elif not args.json and plan.install_plan:
        print("'--apply'를 지정하면 위 단계를 자동으로 실행합니다.")

    return 0


def handle_hardware_telemetry(args: argparse.Namespace) -> int:
    service = _hardware_service_from_args(args)
    samples: List[TelemetrySample] = []
    for index in range(max(args.samples, 1)):
        samples.append(service.capture_telemetry())
        if index < args.samples - 1 and args.interval > 0:
            time.sleep(args.interval)

    if args.json:
        payload = [asdict(sample) for sample in samples]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for sample in samples:
            print(
                f"[{datetime.fromtimestamp(sample.timestamp)}] CPU {sample.cpu_utilisation}% | "
                f"Mem {sample.memory_used_mb}/{sample.memory_total_mb} MB | "
                f"Disk {sample.disk_free_gb}/{sample.disk_total_gb} GB"
            )
            if sample.gpu_utilisation is not None:
                print(
                    f"   GPU {sample.gpu_utilisation}% | "
                    f"VRAM {sample.gpu_memory_used_mb}/{sample.gpu_memory_total_mb} MB"
                )
    return 0


def handle_scheduler_list(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    blueprints = service.list_blueprints()
    if args.json:
        print(json.dumps({"blueprints": blueprints}, indent=2, ensure_ascii=False))
    else:
        if not blueprints:
            print("등록된 블루프린트가 없습니다.")
        else:
            print("사용 가능한 블루프린트:")
            for name in blueprints:
                print(f" - {name}")
    return 0


def handle_scheduler_run(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    extra_vars = _parse_string_pairs(args.extra)
    try:
        result = service.run_blueprint(
            args.name,
            extra_vars=extra_vars,
            dry_run=args.dry_run,
            tags=args.tags,
        )
    except SchedulerError as exc:
        print(f"블루프린트 실행 실패: {exc}", file=sys.stderr)
        return 1

    payload = {
        "name": args.name,
        "blueprint_path": str(result.path),
        "command": result.command,
        "dry_run": result.dry_run,
        "extra_vars": result.extra_vars,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "executed_at": result.executed_at.isoformat(),
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        command_preview = " ".join(result.command)
        print(f"실행 명령: {command_preview}")
        if result.returncode is not None:
            print(f"결과 코드: {result.returncode}")
        if result.stdout.strip():
            print("--- stdout ---")
            print(result.stdout.rstrip())
        if result.stderr.strip():
            print("--- stderr ---", file=sys.stderr)
            print(result.stderr.rstrip(), file=sys.stderr)
        if args.dry_run and shutil.which("ansible-playbook") is None:
            print("[info] ansible-playbook이 설치되어 있지 않아 시뮬레이션 결과만 제공합니다.")
    return 0


def handle_scheduler_job(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    job_args = _normalise_remainder(args.args)
    if not job_args:
        raise ConfigError("sbatch에 전달할 인수를 하나 이상 제공해야 합니다.")
    try:
        result = service.submit_job(job_args, dry_run=args.dry_run)
    except SchedulerError as exc:
        print(f"작업 제출 실패: {exc}", file=sys.stderr)
        return 1
    if result.simulated:
        print(f"[simulated] sbatch {' '.join(job_args)}")
        print(f"가상 작업 ID: {result.job_id}")
    else:
        print(result.stdout.strip() or f"제출된 작업 ID: {result.job_id}")
    return 0


def handle_scheduler_status(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    status_args = _normalise_remainder(args.args)
    if not status_args:
        status_args = ["--noheader", "--format=%i|%j|%P|%T|%M"]
    try:
        output = service.job_status(status_args)
    except SchedulerError as exc:
        print(f"상태 조회 실패: {exc}", file=sys.stderr)
        return 1
    if args.json:
        rows: List[Dict[str, object]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            if "|" in line:
                job_id, name, partition, state, elapsed = (line.split("|") + [""] * 5)[:5]
                rows.append(
                    {
                        "job_id": job_id.strip(),
                        "name": name.strip(),
                        "partition": partition.strip(),
                        "state": state.strip(),
                        "elapsed": elapsed.strip(),
                    }
                )
            else:
                rows.append({"raw": line.strip()})
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        text = output.strip()
        if text:
            print(text)
        else:
            print("현재 큐가 비어 있습니다.")
    return 0


def handle_scheduler_cancel(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    extra = [item for item in args.extra if item != "--"]
    try:
        service.cancel_job(args.job_id, extra)
    except SchedulerError as exc:
        print(f"작업 취소 실패: {exc}", file=sys.stderr)
        return 1
    print(f"작업 {args.job_id}가 취소되었습니다.")
    return 0


def handle_scheduler_targets(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    targets = service.collect_targets()
    if args.json:
        print(json.dumps({"targets": targets}, indent=2, ensure_ascii=False))
    else:
        if not targets:
            print("등록된 대상이 없습니다.")
        else:
            print("감지된 대상:")
            for target in targets:
                print(f" - {target}")
    return 0


def handle_scheduler_window_create(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    metadata = _parse_metadata_pairs(args.meta)
    try:
        window = service.create_window(
            args.name,
            duration_minutes=args.duration,
            targets=args.targets,
            metadata=metadata,
        )
    except SchedulerError as exc:
        print(f"정비 윈도우 생성 실패: {exc}", file=sys.stderr)
        return 1
    payload = {
        "name": window.name,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "targets": window.targets,
        "metadata": window.metadata,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"정비 윈도우 '{window.name}'이 생성되었습니다 → {window.start} ~ {window.end}"
        )
        if window.targets:
            print(f"대상: {', '.join(window.targets)}")
    return 0


def handle_scheduler_window_list(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    windows = service.list_windows()
    payload = [
        {
            "name": window.name,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "targets": window.targets,
            "metadata": window.metadata,
        }
        for window in windows
    ]
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if not windows:
            print("등록된 정비 윈도우가 없습니다.")
        for window in windows:
            target_label = ",".join(window.targets) if window.targets else "-"
            print(
                f" - {window.name}: {window.start} → {window.end} (targets={target_label})"
            )
    return 0


def handle_scheduler_window_close(args: argparse.Namespace) -> int:
    service = _scheduler_service_from_args(args)
    closed = service.close_window(args.name)
    if not closed:
        print(f"이름이 '{args.name}'인 윈도우를 찾을 수 없습니다.", file=sys.stderr)
        return 1
    print(f"정비 윈도우 '{args.name}'이 종료되었습니다.")
    return 0


def handle_network_list(args: argparse.Namespace) -> int:
    service = _network_service_from_args(args)
    profiles = [service.get_profile(name) for name in service.list_profiles()]
    if args.json:
        print(
            json.dumps(
                [profile.to_dict() for profile in profiles], indent=2, ensure_ascii=False
            )
        )
    else:
        if not profiles:
            print("등록된 네트워크 프로파일이 없습니다.")
        for profile in profiles:
            print(f" - {profile.name}: {profile.description or '(no description)'}")
            if profile.interfaces:
                print(f"    interfaces: {', '.join(profile.interfaces)}")
            if profile.vlans:
                print(
                    "    vlans: "
                    + ", ".join(
                        f"{v['parent']}:{v['id']}" + (f"@{v['address']}" if v.get('address') else "")
                        for v in profile.vlans
                    )
                )
            if profile.qos:
                print(
                    "    qos: "
                    + ", ".join(
                        f"{policy.interface}:{policy.rate_limit_mbps or '∞'}Mbps"
                        for policy in profile.qos
                    )
                )
    return 0


def handle_network_save(args: argparse.Namespace) -> int:
    service = _network_service_from_args(args)
    vlans = [_parse_vlan_definition(value) for value in args.vlan]
    qos = [_parse_qos_definition(value) for value in args.qos]
    metadata = _parse_metadata_pairs(args.metadata)
    profile = NetworkProfile(
        name=args.name,
        description=args.description or "",
        interfaces=args.interfaces,
        vlans=vlans,
        qos=qos,
        firewall_rules=args.firewall,
        metadata=metadata,
    )
    try:
        service.save_profile(profile)
    except NetworkAutomationError as exc:
        print(f"프로파일 저장 실패: {exc}", file=sys.stderr)
        return 1
    print(f"네트워크 프로파일 '{profile.name}'이 저장되었습니다.")
    return 0


def handle_network_apply(args: argparse.Namespace) -> int:
    service = _network_service_from_args(args)
    try:
        commands = service.apply_profile(args.name, dry_run=args.dry_run)
    except NetworkAutomationError as exc:
        print(f"프로파일 적용 실패: {exc}", file=sys.stderr)
        return 1
    payload = {"commands": commands, "dry_run": args.dry_run}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if args.dry_run:
            print("다음 명령이 실행될 예정입니다:")
        for command in commands:
            print(f"   $ {command}")
        if not args.dry_run:
            print("프로파일이 성공적으로 적용되었습니다.")
    return 0


def handle_network_delete(args: argparse.Namespace) -> int:
    service = _network_service_from_args(args)
    removed = service.delete_profile(args.name)
    if not removed:
        print(f"프로파일 '{args.name}'을(를) 찾을 수 없습니다.", file=sys.stderr)
        return 1
    print(f"프로파일 '{args.name}'이 삭제되었습니다.")
    return 0


def handle_network_snapshot(args: argparse.Namespace) -> int:
    service = _network_service_from_args(args)
    try:
        output = service.snapshot_interfaces()
    except NetworkAutomationError as exc:
        print(f"스냅샷 실패: {exc}", file=sys.stderr)
        return 1
    print(output.rstrip())
    return 0


def handle_network_qos(args: argparse.Namespace) -> int:
    service = _network_service_from_args(args)
    policy = _parse_qos_definition(args.definition)
    try:
        commands = service.apply_qos(policy, dry_run=args.dry_run)
    except NetworkAutomationError as exc:
        print(f"QoS 적용 실패: {exc}", file=sys.stderr)
        return 1
    payload = {"commands": commands, "dry_run": args.dry_run}
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if args.dry_run:
            print("시뮬레이션 모드: 다음 명령이 실행됩니다")
        for command in commands:
            print(f"   $ {command}")
        if not args.dry_run:
            print("QoS 정책이 적용되었습니다.")
    return 0


def handle_cluster_snapshot(args: argparse.Namespace) -> int:
    service = _cluster_service_from_args(args)
    try:
        report = service.snapshot()
    except ClusterHealthError as exc:
        print(f"헬스 스냅샷 실패: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_health_report(report)
    return 0


def handle_cluster_watch(args: argparse.Namespace) -> int:
    service = _cluster_service_from_args(args)
    try:
        iterator = service.watch(interval=args.interval, limit=args.limit)
        for report in iterator:
            if args.json:
                print(json.dumps(report.to_dict(), ensure_ascii=False))
            else:
                _print_health_report(report)
                print("-" * 60)
    except ClusterHealthError as exc:
        print(f"헬스 모니터링 실패: {exc}", file=sys.stderr)
        return 1
    return 0


def _interactive_loop(
    client: ChatClient,
    base_messages: List[Dict[str, object]],
    args: argparse.Namespace,
    response_format: Optional[Dict[str, object]],
    extra_options: Dict[str, object],
) -> int:
    conversation = list(base_messages)
    print("Starting interactive session. Type :help for commands, :reset to clear context, :quit to exit.")
    while True:
        try:
            prompt = input("you> ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        if not prompt.strip():
            continue

        if prompt.startswith(":"):
            command = prompt[1:].strip().lower()
            if command in {"quit", "q", "exit"}:
                break
            if command == "help":
                print("Commands: :help, :reset, :quit")
                continue
            if command == "reset":
                conversation = list(base_messages)
                print("Context cleared.")
                continue

        conversation.append({"role": "user", "content": prompt})
        completion = client.create_chat_completion(
            conversation,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            response_format=response_format,
            extra_options=extra_options,
        )
        conversation.append({"role": "assistant", "content": completion.content})
        _emit_completion(completion, args)
        if args.history:
            _append_history(args.history, client.settings.name, conversation, completion)
    return 0


def _emit_completion(completion: ChatCompletion, args: argparse.Namespace) -> None:
    if args.json:
        print(json.dumps(completion.raw, indent=2))
        return

    text = completion.content.strip()
    if text:
        print(text)
    usage_text = format_usage(completion.usage)
    if usage_text:
        print(f"[usage] {usage_text}")


def _append_history(
    path: str,
    provider_name: str,
    messages: Iterable[Dict[str, object]],
    completion: ChatCompletion,
) -> None:
    history_path = Path(path).expanduser()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": provider_name,
        "messages": list(messages),
        "response": completion.raw,
    }
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry))
        handle.write("\n")


def _print_assist_summary(result, *, executed: bool) -> None:
    intent = result.intent
    try:
        confidence = f"{float(intent.confidence):.2f}"
    except (TypeError, ValueError):
        confidence = "?"

    print("=== Ainux Assist ===")
    print(f"요청: {intent.raw_input}")
    print(f"이해한 작업: {intent.action} (신뢰도 {confidence})")
    if intent.reasoning:
        print(f"사유: {intent.reasoning}")
    if intent.parameters:
        print("추론된 매개변수:", json.dumps(intent.parameters, ensure_ascii=False))

    if result.plan.notes:
        print(f"계획 메모: {result.plan.notes}")

    if result.plan.steps:
        print("\n계획:")
        for index, step in enumerate(result.plan.steps, 1):
            description = (step.description or "").strip() or step.action
            print(f"  {index}. {description}")
            if step.action and step.action != description:
                print(f"     ↳ action: {step.action}")
            if step.parameters:
                print("     ↳ parameters:", json.dumps(step.parameters, ensure_ascii=False))
    else:
        print("\n계획: (없음)")

    if result.safety.warnings:
        print("\n안전 경고:")
        for warning in result.safety.warnings:
            print(f"  - {warning}")
    if result.safety.rationale:
        print(f"안전 검토 근거: {result.safety.rationale}")
    if result.safety.blocked_steps:
        print("\n차단된 단계:")
        for step in result.safety.blocked_steps:
            description = step.description or step.action
            print(f"  - {step.id}: {description}")

    if executed:
        print("\n실행 결과:")
        if result.execution:
            for entry in result.execution:
                line = f"  - {entry.step_id}: {entry.status}"
                if entry.output:
                    line += f" → {entry.output}"
                if entry.error:
                    line += f" (오류: {entry.error})"
                print(line)
        else:
            print("  - 실행할 단계가 없었습니다.")
    else:
        print("\n실행은 건너뛰었습니다. (--dry-run)")

    message = next((review.message for review in reversed(result.reviews) if review.message), None)
    if message:
        print(f"\n추가 안내: {message}")


def _orchestration_result_to_dict(result) -> Dict[str, object]:
    return {
        "intent": {
            "raw_input": result.intent.raw_input,
            "action": result.intent.action,
            "confidence": result.intent.confidence,
            "parameters": result.intent.parameters,
            "reasoning": result.intent.reasoning,
        },
        "plan": {
            "notes": result.plan.notes,
            "steps": [
                {
                    "id": step.id,
                    "action": step.action,
                    "description": step.description,
                    "parameters": step.parameters,
                    "depends_on": step.depends_on,
                }
                for step in result.plan.steps
            ],
        },
        "safety": {
            "approved_steps": [step.id for step in result.safety.approved_steps],
            "blocked_steps": [step.id for step in result.safety.blocked_steps],
            "warnings": result.safety.warnings,
            "rationale": result.safety.rationale,
        },
        "execution": [
            {
                "step_id": entry.step_id,
                "status": entry.status,
                "output": entry.output,
                "error": entry.error,
            }
            for entry in result.execution
        ],
    }


def _print_orchestration_result(payload: Dict[str, object]) -> None:
    intent = payload.get("intent", {})
    confidence = intent.get("confidence")
    if confidence is not None:
        try:
            confidence_str = f"{float(confidence):.2f}"
        except (TypeError, ValueError):
            confidence_str = "?"
    else:
        confidence_str = "?"
    print("Intent →", intent.get("action"), f"(confidence={confidence_str})")
    if intent.get("reasoning"):
        print(f"  reasoning: {intent['reasoning']}")
    if intent.get("parameters"):
        print(f"  parameters: {json.dumps(intent['parameters'], ensure_ascii=False)}")

    plan = payload.get("plan", {})
    print("\nPlan Steps:")
    for step in plan.get("steps", []):
        print(f"- [{step['id']}] {step['action']}: {step['description']}")
        if step.get("depends_on"):
            print(f"    depends_on: {', '.join(step['depends_on'])}")
        if step.get("parameters"):
            print(
                "    parameters:",
                json.dumps(step["parameters"], ensure_ascii=False),
            )

    safety = payload.get("safety", {})
    print("\nSafety:")
    print("  approved:", ", ".join(safety.get("approved_steps", [])) or "(none)")
    print("  blocked:", ", ".join(safety.get("blocked_steps", [])) or "(none)")
    if safety.get("warnings"):
        for warning in safety["warnings"]:
            print("  warning:", warning)
    if safety.get("rationale"):
        print("  rationale:", safety["rationale"])

    print("\nExecution Results:")
    for entry in payload.get("execution", []):
        line = f"- [{entry['step_id']}] {entry['status']}"
        if entry.get("output"):
            line += f" → {entry['output']}"
        if entry.get("error"):
            line += f" (error: {entry['error']})"
        print(line)
    if not payload.get("execution"):
        print("- (skipped)")


def _parse_response_format(value: Optional[str]) -> Optional[Dict[str, object]]:
    if not value:
        return None
    lowered = value.strip().lower()
    if lowered == "json":
        return {"type": "json_object"}
    if lowered == "text":
        return {"type": "text"}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Unable to decode --response-format: {exc}")
    if not isinstance(parsed, dict):
        raise ConfigError("--response-format must decode to a JSON object")
    return parsed


def _parse_extra_options(pairs: List[str]) -> Dict[str, object]:
    options: Dict[str, object] = {}
    for item in pairs:
        if "=" not in item:
            raise ConfigError("--extra-option values must be in KEY=VALUE format")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise ConfigError("--extra-option key cannot be empty")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        options[key] = value
    return options


def _collect_extra_headers(pairs: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ConfigError("--extra-header values must be in KEY=VALUE format")
        key, value = item.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers


def _parse_string_pairs(pairs: List[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise ConfigError("값은 KEY=VALUE 형식이어야 합니다.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError("키는 비어 있을 수 없습니다.")
        values[key] = value.strip()
    return values


def _parse_metadata_pairs(pairs: List[str]) -> Dict[str, object]:
    metadata: Dict[str, object] = {}
    for item in pairs:
        if "=" not in item:
            raise ConfigError("메타데이터는 KEY=VALUE 형식이어야 합니다.")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError("메타데이터 키는 비어 있을 수 없습니다.")
        metadata[key] = _parse_json_arg(raw_value.strip())
    return metadata


def _parse_vlan_definition(value: str) -> Dict[str, object]:
    if ":" not in value:
        raise ConfigError("VLAN 정의는 parent:id[:address] 형식이어야 합니다.")
    parent, rest = value.split(":", 1)
    if not parent:
        raise ConfigError("VLAN parent 인터페이스를 지정해야 합니다.")
    if ":" in rest:
        vlan_id_part, address = rest.split(":", 1)
    else:
        vlan_id_part, address = rest, None
    try:
        vlan_id = int(vlan_id_part)
    except ValueError as exc:
        raise ConfigError("VLAN ID는 정수여야 합니다.") from exc
    entry: Dict[str, object] = {"parent": parent, "id": vlan_id}
    if address:
        entry["address"] = address
    return entry


def _parse_qos_definition(value: str) -> QoSPolicy:
    if ":" not in value:
        raise ConfigError("QoS 정의는 iface:rate[:burst] 형식이어야 합니다.")
    interface, rest = value.split(":", 1)
    if not interface:
        raise ConfigError("QoS 인터페이스 이름을 지정해야 합니다.")
    if ":" in rest:
        rate_part, burst_part = rest.split(":", 1)
    else:
        rate_part, burst_part = rest, None
    rate = _parse_rate_value(rate_part)
    burst = _parse_rate_value(burst_part) if burst_part else None
    return QoSPolicy(interface=interface, rate_limit_mbps=rate, burst_mbps=burst)


def _parse_rate_value(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    for suffix in ["mbit", "mbps", "m"]:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    try:
        return int(float(cleaned))
    except ValueError as exc:
        raise ConfigError(f"잘못된 대역폭 값: {raw}") from exc


def _normalise_remainder(values: Sequence[str]) -> List[str]:
    items = [value for value in values if value]
    if items and items[0] == "--":
        items = items[1:]
    return items


def _print_health_report(report: HealthReport) -> None:
    print(f"[{report.timestamp}] CPU load: {', '.join(f'{v:.2f}' for v in report.load_average)}")
    print(
        f"  Memory: {report.memory.get('available_mb', 0):.0f} MB free / "
        f"{report.memory.get('total_mb', 0):.0f} MB total"
    )
    print(
        f"  Disk: {report.disk.get('used_gb', 0)} GB used / {report.disk.get('total_gb', 0)} GB"
    )
    if report.gpus:
        for gpu in report.gpus:
            print(
                "  GPU {index}: {name} util={util}% vram={used}/{total} MB".format(
                    index=gpu.get("index"),
                    name=gpu.get("name"),
                    util=gpu.get("utilisation_percent", 0),
                    used=gpu.get("memory_used_mb", 0),
                    total=gpu.get("memory_total_mb", 0),
                )
            )
    if report.scheduler_queue:
        print("  Scheduler queue:")
        for job in report.scheduler_queue:
            print(
                f"    {job.get('job_id')} {job.get('name')} {job.get('state')} ({job.get('elapsed')})"
            )
    if report.network_interfaces:
        print("  Network:")
        for iface in report.network_interfaces[:5]:
            print(
                f"    {iface.get('name')}: rx {iface.get('rx_bytes')} bytes, tx {iface.get('tx_bytes')} bytes"
            )


def _prompt_default(label: str, default: str, non_interactive: bool) -> str:
    if non_interactive:
        return default
    user_input = input(f"{label} [{default}]: ").strip()
    return user_input or default


def _prompt_optional(label: str, non_interactive: bool) -> Optional[str]:
    if non_interactive:
        return None
    value = input(f"{label} (press enter to skip): ").strip()
    return value or None


def _prompt_secret(label: str, non_interactive: bool) -> str:
    if non_interactive:
        raise ConfigError(f"{label} must be provided in non-interactive mode")
    value = getpass(f"{label}: ").strip()
    if not value:
        raise ConfigError(f"{label} cannot be empty")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except ChatClientError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 2
    except HardwareAutomationError as exc:
        print(f"Hardware automation error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


__all__ = ["build_parser", "main"]
