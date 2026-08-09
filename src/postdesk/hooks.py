from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigError


def _template(value: Any, name: str, required: str, *, optional: str | None = None) -> list[str]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a TOML array of strings.", path=f"hooks.{name}")
    if value.count(required) != 1:
        raise ConfigError(
            f"{name} must contain one standalone {required} argument.",
            path=f"hooks.{name}",
        )
    allowed = {required}
    if optional:
        allowed.add(optional)
    for item in value:
        if "{" in item or "}" in item:
            if item not in allowed:
                raise ConfigError(
                    f"{name} contains an unknown or embedded placeholder.",
                    path=f"hooks.{name}",
                )
    return list(value)


def hook_timeout(hooks: dict[str, Any]) -> float:
    raw = hooks.get("timeout_seconds", 30)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("timeout_seconds must be a number.", path="hooks.timeout_seconds") from exc
    if value <= 0:
        raise ConfigError("timeout_seconds must be positive.", path="hooks.timeout_seconds")
    return value


def resolve_media(value: str, hooks: dict[str, Any]) -> str:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme:
        raise ConfigError("Media must be a local path or HTTP(S) URL.", path="media")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ConfigError("Local media file does not exist.", path=str(path))
    template = _template(hooks.get("host_cmd", []), "host_cmd", "{path}")
    if not template:
        raise ConfigError("Local media requires hooks.host_cmd.", path="hooks.host_cmd")
    command = [str(path) if item == "{path}" else item for item in template]
    try:
        result = subprocess.run(
            command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=hook_timeout(hooks),
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigError("host_cmd timed out.", path="hooks.host_cmd") from exc
    except OSError as exc:
        raise ConfigError(f"host_cmd could not run: {exc}", path="hooks.host_cmd") from exc
    if result.returncode != 0:
        detail = " ".join(result.stderr.split())[:500]
        raise ConfigError(
            f"host_cmd exited {result.returncode}: {detail or 'no error output'}",
            path="hooks.host_cmd",
        )
    url = result.stdout.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("host_cmd did not return an HTTP(S) URL.", path="hooks.host_cmd")
    return url


def notify(message: str, hooks: dict[str, Any], *, file: str | None = None) -> str | None:
    try:
        template = _template(hooks.get("notify_cmd", []), "notify_cmd", "{message}", optional="{file}")
        if not template:
            return None
        command = [
            message if item == "{message}" else (file or "") if item == "{file}" else item
            for item in template
        ]
        result = subprocess.run(
            command,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=hook_timeout(hooks),
        )
        if result.returncode:
            return f"notify_cmd exited {result.returncode}: {' '.join(result.stderr.split())[:500]}"
        return None
    except (ConfigError, OSError, subprocess.SubprocessError) as exc:
        return f"notify_cmd failed: {exc}"[:500]

