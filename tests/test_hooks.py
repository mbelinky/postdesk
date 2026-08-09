from __future__ import annotations

import sys

import pytest

from postdesk.errors import ConfigError
from postdesk.hooks import notify, resolve_media


def test_host_cmd_success(tmp_path):
    media = tmp_path / "image.jpg"
    media.write_bytes(b"fixture")
    hooks = {
        "host_cmd": [sys.executable, "-c", "import sys; print('https://media.example/' + sys.argv[1].split('/')[-1])", "{path}"],
        "timeout_seconds": 1,
    }
    assert resolve_media(str(media), hooks) == "https://media.example/image.jpg"


def test_host_cmd_nonzero_fails_publish(tmp_path):
    media = tmp_path / "image.jpg"
    media.write_bytes(b"fixture")
    hooks = {
        "host_cmd": [sys.executable, "-c", "import sys; print('fixture failure', file=sys.stderr); raise SystemExit(7)", "{path}"],
        "timeout_seconds": 1,
    }
    with pytest.raises(ConfigError, match="exited 7: fixture failure"):
        resolve_media(str(media), hooks)


def test_host_cmd_timeout(tmp_path):
    media = tmp_path / "image.jpg"
    media.write_bytes(b"fixture")
    hooks = {
        "host_cmd": [sys.executable, "-c", "import time; time.sleep(1)", "{path}"],
        "timeout_seconds": 0.01,
    }
    with pytest.raises(ConfigError, match="timed out"):
        resolve_media(str(media), hooks)


def test_notify_is_never_fatal():
    error = notify("fixture", {"notify_cmd": ["/missing/notifier", "{message}"], "timeout_seconds": 1})
    assert error and error.startswith("notify_cmd failed")

