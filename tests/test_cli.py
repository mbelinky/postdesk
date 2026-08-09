from __future__ import annotations

import json

import pytest

import postdesk.cli as cli
from postdesk.drivers.facebook import FacebookDriver
from postdesk.drivers.instagram import InstagramDriver
from postdesk.errors import ApiError

from .helpers import FixtureTransport


def invoke(capsys, *args):
    status = cli.main(list(args))
    captured = capsys.readouterr()
    output = captured.out or captured.err
    return status, json.loads(output)


def test_describe_is_machine_readable(capsys):
    status, payload = invoke(capsys, "describe", "--json")
    assert status == 0
    assert payload["tool"] == "postdesk"
    assert payload["at_most_once"]["durable_attempt_before_network"] is True
    assert payload["exit_codes"]["4"] == "network or API error"


def test_init_demo_capabilities_and_capability_gate(tmp_path, capsys):
    status, initialized = invoke(capsys, "init", "--store", str(tmp_path), "--json")
    assert status == 0
    assert initialized["ok"] is True
    status, matrix = invoke(capsys, "capabilities", "--store", str(tmp_path), "--tenant", "demo", "--json")
    assert status == 0
    assert set(matrix["channels"]) == {"facebook", "instagram"}
    assert matrix["channels"]["facebook"]["features"]["edit"] is True
    assert matrix["channels"]["instagram"]["features"]["edit"] is False

    caption = tmp_path / "caption.txt"
    caption.write_text("Not valid for a story", encoding="utf-8")
    status, error = invoke(
        capsys,
        "queue",
        "--store",
        str(tmp_path),
        "add",
        "--tenant",
        "demo",
        "--channel",
        "instagram",
        "--kind",
        "story",
        "--media",
        "https://media.example/story.jpg",
        "--caption-file",
        str(caption),
        "--json",
    )
    assert status == 2
    assert error["error"]["kind"] == "invalid_input"
    assert "instagram.story.caption" in error["error"]["detail"]


def test_api_failure_is_exit_four_with_structured_detail(tmp_path, capsys, monkeypatch):
    invoke(capsys, "init", "--store", str(tmp_path), "--json")
    monkeypatch.setenv("POSTDESK_DEMO_FACEBOOK_TOKEN", "fixture-token")
    facebook = FacebookDriver(FixtureTransport([ApiError("Recorded API denial", path="fixture", definitive=True)]))
    monkeypatch.setattr(cli, "default_drivers", lambda: {"instagram": InstagramDriver(), "facebook": facebook})
    caption = tmp_path / "caption.txt"
    caption.write_text("Fixture post", encoding="utf-8")
    status, payload = invoke(
        capsys,
        "publish",
        "--store",
        str(tmp_path),
        "--tenant",
        "demo",
        "--channel",
        "facebook",
        "--kind",
        "text",
        "--caption-file",
        str(caption),
        "--external-ref",
        "api-error",
        "--json",
    )
    assert status == 4
    assert payload["error"] == {
        "kind": "network_api",
        "detail": "Recorded API denial",
        "path": "results.1",
    }


@pytest.mark.parametrize(
    "args",
    [
        ("queue", "list", "--json"),
        ("tenant", "list", "--json"),
    ],
)
def test_store_errors_are_structured(capsys, args, monkeypatch):
    monkeypatch.delenv("POSTDESK_STORE", raising=False)
    status, payload = invoke(capsys, *args)
    assert status == 3
    assert payload["error"]["kind"] == "config"

