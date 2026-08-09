from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError, InvalidInput

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

DEMO_CONFIG = """# Environment variable names only. Never put secrets in this file.
[hooks]
host_cmd = []
notify_cmd = []
timeout_seconds = 10

[channels.instagram]
ig_user_id = "demo-instagram-user"
token_env = "POSTDESK_DEMO_INSTAGRAM_TOKEN"

[channels.facebook]
page_id = "demo-facebook-page"
token_env = "POSTDESK_DEMO_FACEBOOK_TOKEN"
"""


@dataclass(frozen=True)
class TenantConfig:
    name: str
    path: Path
    hooks: dict[str, Any]
    channels: dict[str, dict[str, Any]]

    def channel(self, channel: str, *, dry_run: bool) -> dict[str, str]:
        raw = self.channels.get(channel)
        if not isinstance(raw, dict):
            raise ConfigError(
                f"Tenant {self.name!r} has no {channel!r} channel.",
                path=f"channels.{channel}",
            )
        token_env = str(raw.get("token_env") or "").strip()
        if not token_env:
            raise ConfigError("token_env is required.", path=f"channels.{channel}.token_env")
        token = os.environ.get(token_env, "").strip()
        if not token and not dry_run:
            raise ConfigError(
                f"Environment variable {token_env} is not set.",
                path=f"channels.{channel}.token_env",
            )
        credentials = {key: str(value) for key, value in raw.items() if key != "token_env"}
        credentials["token"] = token if token else f"<{token_env}>"
        credentials["token_env"] = token_env
        return credentials


def validate_tenant_name(name: str) -> str:
    clean = name.strip()
    if not TENANT_RE.fullmatch(clean):
        raise InvalidInput(
            "Tenant names use lowercase letters, numbers, underscores, and hyphens.",
            path="tenant",
        )
    return clean


def tenants_dir(store: Path) -> Path:
    configured = os.environ.get("POSTDESK_TENANTS_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else store.parent / "tenants"


def add_tenant(store: Path, name: str, *, demo: bool = False) -> Path:
    clean = validate_tenant_name(name)
    directory = tenants_dir(store) / clean
    config_path = directory / "config.toml"
    if config_path.exists():
        raise InvalidInput(f"Tenant {clean!r} already exists.", path="tenant")
    directory.mkdir(parents=True, exist_ok=False)
    config_path.write_text(DEMO_CONFIG if demo else tenant_template(clean), encoding="utf-8")
    return config_path


def tenant_template(name: str) -> str:
    prefix = re.sub(r"[^A-Z0-9]", "_", name.upper())
    return DEMO_CONFIG.replace("demo-instagram-user", "replace-me").replace(
        "demo-facebook-page", "replace-me"
    ).replace("POSTDESK_DEMO_", f"POSTDESK_{prefix}_")


def load_tenant(store: Path, name: str) -> TenantConfig:
    clean = validate_tenant_name(name)
    path = tenants_dir(store) / clean / "config.toml"
    if not path.is_file():
        raise ConfigError(f"Tenant {clean!r} was not found.", path=str(path))
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Cannot read tenant config: {exc}", path=str(path)) from exc
    hooks = data.get("hooks", {})
    channels = data.get("channels", {})
    if not isinstance(hooks, dict) or not isinstance(channels, dict):
        raise ConfigError("hooks and channels must be TOML tables.", path=str(path))
    return TenantConfig(clean, path, hooks, channels)


def list_tenants(store: Path) -> list[str]:
    root = tenants_dir(store)
    if not root.exists():
        return []
    return sorted(path.parent.name for path in root.glob("*/config.toml") if path.is_file())


def public_config(config: TenantConfig) -> dict[str, Any]:
    return {
        "name": config.name,
        "path": str(config.path),
        "hooks": config.hooks,
        "channels": config.channels,
    }

