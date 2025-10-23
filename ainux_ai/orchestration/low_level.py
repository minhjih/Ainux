"""Helpers for synthesizing low-level launchers from natural-language requests."""

from __future__ import annotations

import os
import re
import shutil
from typing import Dict, List, Optional, Sequence, Tuple


def prepare_low_level_parameters(parameters: Dict[str, object]) -> Dict[str, object]:
    """Return a copy of *parameters* with synthesized low-level source code."""

    params = dict(parameters or {})
    metadata: Dict[str, object] = dict(params.get("_ainux_low_level") or {})
    metadata.setdefault("synthesized_source", False)
    metadata.setdefault("target", None)

    raw_source = params.get("source") or params.get("code")
    if isinstance(raw_source, str) and raw_source.strip():
        metadata["provided_source"] = True
        params["_ainux_low_level"] = metadata
        return params
    if raw_source and not isinstance(raw_source, str):
        metadata["provided_source"] = True
        params["_ainux_low_level"] = metadata
        return params

    request = str(params.get("original_request") or "").strip()
    candidate_token: Optional[str] = None
    target = _extract_explicit_target(params)
    if target:
        candidate_token = target[0]
    if not target and request:
        inferred = infer_low_level_target(request)
        if inferred:
            target = inferred
            candidate_token = inferred[0]

    if not target:
        candidate = params.get("target") or params.get("program") or params.get("executable")
        if isinstance(candidate, str) and candidate.strip():
            candidate_token = candidate.strip()
        if candidate_token:
            metadata["candidate"] = candidate_token
        params["_ainux_low_level"] = metadata
        return params

    executable, extra_args = target
    metadata["target"] = {"executable": executable, "args": list(extra_args)}
    if executable:
        metadata["candidate"] = executable
    language = str(params.get("language") or "assembly").lower()
    params.setdefault("args", list(extra_args))
    if language in {"asm", "assembly", "machine", "binary"}:
        params["language"] = "assembly"
        params["source"] = generate_assembly_launcher(executable, extra_args)
        metadata["synthesized_source"] = True
    else:
        params["language"] = "c"
        params["source"] = generate_c_launcher(executable, extra_args)
        metadata["synthesized_source"] = True
    params.pop("code", None)
    params["_ainux_low_level"] = metadata
    return params


def infer_low_level_target(request: str) -> Optional[Tuple[str, List[str]]]:
    """Infer an executable path from the natural-language *request*."""

    lowered = request.lower()

    keyword_targets = {
        "firefox": (["firefox", "/usr/bin/firefox"], "/usr/bin/firefox"),
        "terminal": (
            [
                "gnome-terminal",
                "x-terminal-emulator",
                "/usr/bin/gnome-terminal",
                "xfce4-terminal",
            ],
            "/usr/bin/gnome-terminal",
        ),
        "gnome-terminal": (["gnome-terminal", "/usr/bin/gnome-terminal"], "/usr/bin/gnome-terminal"),
        "chrome": (["google-chrome", "/usr/bin/google-chrome"], "/usr/bin/google-chrome"),
        "chromium": (["chromium-browser", "chromium", "/usr/bin/chromium"], "/usr/bin/chromium"),
        "code": (["code", "/usr/bin/code"], "/usr/bin/code"),
    }

    for keyword, (candidates, fallback) in keyword_targets.items():
        if keyword in lowered:
            resolved = _resolve_executable(candidates) or fallback
            if resolved:
                return resolved, []

    command_match = re.search(
        r"(?:execute|excute|run|launch|start|open|실행|열어|켜)\s+([\w.-]+)",
        lowered,
    )
    if command_match:
        candidate = command_match.group(1)
        resolved = _resolve_executable([candidate])
        if not resolved:
            resolved = _default_executable(candidate)
        if resolved:
            return resolved, []

    skip_tokens = {
        "assembly",
        "asm",
        "machine",
        "code",
        "by",
        "using",
        "please",
        "the",
        "this",
        "request",
        "program",
        "app",
        "application",
        "어셈",
        "기계어",
        "실행",
        "열어",
        "켜",
        "줘",
        "좀",
        "으로",
        "해서",
        "excute",
    }

    for token in re.findall(r"[\w.-]+", lowered):
        if token in skip_tokens or len(token) < 2:
            continue
        resolved = _resolve_executable([token])
        if not resolved:
            resolved = _default_executable(token)
        if resolved:
            return resolved, []

    return None


def generate_assembly_launcher(executable: str, extra_args: Sequence[str]) -> str:
    """Generate an x86_64 assembly stub that launches *executable*."""

    args = [executable, *extra_args]
    escaped_strings = [_escape_assembly_string(value) for value in args]

    lines = [
        ".section .text",
        ".global _start",
        "_start:",
        "    mov $59, %rax",
        "    lea cmd_path(%rip), %rdi",
        "    lea argv_list(%rip), %rsi",
        "    lea env_list(%rip), %rdx",
        "    syscall",
        "    neg %rax",
        "    mov %rax, %rdi",
        "    mov $60, %rax",
        "    syscall",
        "",
        ".section .rodata",
        "cmd_path:",
        f"    .string \"{escaped_strings[0]}\"",
        "argv_list:",
    ]

    for index in range(len(args)):
        if index == 0:
            lines.append("    .quad cmd_path")
        else:
            lines.append(f"    .quad arg_{index}")
    lines.append("    .quad 0")

    lines.append("")
    lines.append("env_list:")
    lines.append("    .quad 0")

    for index, value in enumerate(escaped_strings[1:], start=1):
        lines.append("")
        lines.append(f"arg_{index}:")
        lines.append(f"    .string \"{value}\"")

    lines.append("")
    return "\n".join(lines)


def generate_c_launcher(executable: str, extra_args: Sequence[str]) -> str:
    """Generate a small C program that launches *executable*."""

    args = [executable, *extra_args]
    escaped = [value.replace("\\", "\\\\").replace('"', '\\"') for value in args]
    args_initializer = ", ".join(f'"{value}"' for value in escaped)

    lines = [
        "#include <errno.h>",
        "#include <string.h>",
        "#include <unistd.h>",
        "#include <stdio.h>",
        "",
        "int main(void) {",
        f"    const char *args[] = {{{args_initializer}, NULL}};",
        f"    execvp(\"{escaped[0]}\", (char * const *)args);",
        "    perror(\"execvp\");",
        "    return errno ? (int)errno : 1;",
        "}",
    ]

    return "\n".join(lines)


def _extract_explicit_target(params: Dict[str, object]) -> Optional[Tuple[str, List[str]]]:
    for key in ("executable", "program", "target"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), _coerce_arg_list(params.get("args"))

    command = params.get("command")
    if isinstance(command, (list, tuple)) and command:
        head = str(command[0]).strip()
        rest = [str(item) for item in command[1:] if str(item).strip()]
        if head:
            return head, rest
    if isinstance(command, str) and command.strip():
        parts = [part for part in command.split() if part]
        if parts:
            return parts[0], parts[1:]

    return None


def _coerce_arg_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [part for part in value.split() if part]
    return []


def _resolve_executable(candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _default_executable(token: str) -> Optional[str]:
    token = token.strip()
    if not token or "/" in token:
        return None
    return f"/usr/bin/{token}"


def _escape_assembly_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
