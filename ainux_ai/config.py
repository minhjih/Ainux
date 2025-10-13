"""Configuration helpers for the Ainux AI client."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CONFIG_PATH_ENV = "AINUX_AI_CONFIG_PATH"
ENV_PROVIDER = "AINUX_AI_PROVIDER"
ENV_API_KEY = "AINUX_GPT_API_KEY"
ENV_BASE_URL = "AINUX_GPT_BASE_URL"
ENV_MODEL = "AINUX_GPT_MODEL"
ENV_ORG = "AINUX_GPT_ORG"
ENV_EXTRA_HEADERS = "AINUX_GPT_EXTRA_HEADERS"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


@dataclass
class ProviderSettings:
    """Runtime configuration for a chat completion provider."""

    name: str
    api_key: str
    base_url: str
    model: str
    organization: Optional[str] = None
    extra_headers: Dict[str, str] = field(default_factory=dict)


def _default_config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        base = Path(config_home).expanduser()
    else:
        base = Path.home() / ".config"
    return base / "ainux" / "ai_client.json"


def load_config() -> Dict[str, object]:
    """Load configuration from disk, returning the raw dictionary."""

    path = _default_config_path()
    if not path.exists():
        return {"version": 1, "providers": {}, "default_provider": None}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid configuration JSON at {path}: {exc}")

    if not isinstance(data, dict):
        raise ConfigError(f"Configuration at {path} must be a JSON object")

    data.setdefault("version", 1)
    data.setdefault("providers", {})
    data.setdefault("default_provider", None)
    return data


def save_config(data: Dict[str, object]) -> Path:
    """Persist configuration to disk and return the saved path."""

    path = _default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, indent=2, sort_keys=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(serialized, encoding="utf-8")
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        # Non-fatal on filesystems that disallow chmod (e.g., mounted via CIFS)
        pass
    return path


def upsert_provider(
    name: str,
    api_key: str,
    base_url: str,
    model: str,
    *,
    organization: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    make_default: bool = False,
) -> Path:
    """Create or update a provider entry on disk."""

    if not name:
        raise ConfigError("Provider name must be specified")
    if not api_key:
        raise ConfigError("API key must be provided")
    if not model:
        raise ConfigError("Model must be provided")

    data = load_config()
    providers = data.setdefault("providers", {})
    providers[name] = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "organization": organization,
        "extra_headers": extra_headers or {},
    }

    if make_default or not data.get("default_provider"):
        data["default_provider"] = name

    return save_config(data)


def remove_provider(name: str) -> Path:
    data = load_config()
    providers = data.get("providers", {})
    if name not in providers:
        raise ConfigError(f"Provider '{name}' not found")

    providers.pop(name)
    if data.get("default_provider") == name:
        data["default_provider"] = next(iter(providers), None)
    return save_config(data)


def set_default_provider(name: str) -> Path:
    data = load_config()
    providers = data.get("providers", {})
    if name not in providers:
        raise ConfigError(f"Provider '{name}' not found")
    data["default_provider"] = name
    return save_config(data)


def list_providers() -> List[ProviderSettings]:
    data = load_config()
    providers = []
    for name, meta in data.get("providers", {}).items():
        providers.append(
            ProviderSettings(
                name=name,
                api_key=str(meta.get("api_key", "")),
                base_url=str(meta.get("base_url", "https://api.openai.com/v1")),
                model=str(meta.get("model", "gpt-4o-mini")),
                organization=meta.get("organization") or None,
                extra_headers=dict(meta.get("extra_headers", {})),
            )
        )
    return providers


def _parse_extra_headers(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = {}
        for item in raw.split(","):
            if not item.strip():
                continue
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            decoded[key.strip()] = value.strip()
    if not isinstance(decoded, dict):
        raise ConfigError("Extra headers must be provided as JSON object or comma-delimited key=value pairs")
    return {str(k): str(v) for k, v in decoded.items()}


def resolve_provider(requested: Optional[str] = None) -> ProviderSettings:
    env_api_key = os.environ.get(ENV_API_KEY)
    if env_api_key:
        name = requested or os.environ.get(ENV_PROVIDER) or "env"
        base_url = os.environ.get(ENV_BASE_URL, "https://api.openai.com/v1")
        model = os.environ.get(ENV_MODEL, "gpt-4o-mini")
        organization = os.environ.get(ENV_ORG)
        extra_headers = _parse_extra_headers(os.environ.get(ENV_EXTRA_HEADERS))
        return ProviderSettings(
            name=name,
            api_key=env_api_key,
            base_url=base_url,
            model=model,
            organization=organization,
            extra_headers=extra_headers,
        )

    data = load_config()
    providers = data.get("providers", {})

    provider_name = requested or os.environ.get(ENV_PROVIDER) or data.get("default_provider")
    if not provider_name:
        raise ConfigError(
            "No AI provider configured. Run 'ainux-ai-chat configure' or set AINUX_GPT_API_KEY."
        )

    meta = providers.get(provider_name)
    if not meta:
        raise ConfigError(f"Provider '{provider_name}' not defined in configuration")

    api_key = str(meta.get("api_key", ""))
    if not api_key:
        raise ConfigError(f"Provider '{provider_name}' does not have an API key configured")

    base_url = str(meta.get("base_url", "https://api.openai.com/v1"))
    model = str(meta.get("model", "gpt-4o-mini"))
    organization = meta.get("organization") or None
    extra_headers = dict(meta.get("extra_headers", {}))

    return ProviderSettings(
        name=provider_name,
        api_key=api_key,
        base_url=base_url,
        model=model,
        organization=organization,
        extra_headers=extra_headers,
    )


def update_provider_api_key(
    name: str,
    api_key: str,
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    organization: Optional[str] = None,
    create_missing: bool = False,
    make_default: bool = False,
) -> Tuple[Path, str]:
    """Update (or optionally create) the API key for a provider."""

    if not api_key:
        raise ConfigError("API key must be provided")

    data = load_config()
    providers = data.setdefault("providers", {})

    provider_name = name or data.get("default_provider") or "openai"
    existing = providers.get(provider_name)

    if existing is None:
        if not create_missing:
            raise ConfigError(
                f"Provider '{provider_name}' not found. Run 'ainux-ai-chat configure' or pass --create."
            )
        meta: Dict[str, object] = {
            "api_key": api_key,
            "base_url": base_url or DEFAULT_BASE_URL,
            "model": model or DEFAULT_MODEL,
            "organization": organization or None,
            "extra_headers": {},
        }
    else:
        meta = dict(existing)
        meta["api_key"] = api_key
        if base_url is not None:
            meta["base_url"] = base_url
        if model is not None:
            meta["model"] = model
        if organization is not None:
            meta["organization"] = organization or None

    providers[provider_name] = meta

    if make_default or not data.get("default_provider"):
        data["default_provider"] = provider_name

    path = save_config(data)
    return path, provider_name


def mask_secret(secret: str, visible: int = 4) -> str:
    if not secret:
        return ""
    masked_len = max(len(secret) - visible, 0)
    return "*" * masked_len + secret[-visible:]


def ensure_config_dir() -> Path:
    path = _default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export_config(providers: Iterable[ProviderSettings]) -> Dict[str, object]:
    payload = {"version": 1, "providers": {}, "default_provider": None}
    for provider in providers:
        payload["providers"][provider.name] = {
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "model": provider.model,
            "organization": provider.organization,
            "extra_headers": provider.extra_headers,
        }
    return payload
