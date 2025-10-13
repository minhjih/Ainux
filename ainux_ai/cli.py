"""Command line interface for the Ainux AI GPT client."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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
from .orchestration import AinuxOrchestrator, OrchestrationError
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
