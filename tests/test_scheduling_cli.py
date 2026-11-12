from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def queue_cli(tmp_path):
    def cli(*args, status=0):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "postdesk",
                "--store",
                str(tmp_path),
                *map(str, args),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == status, result.stdout + result.stderr
        payload = json.loads(result.stdout or result.stderr)
        if status == 5:
            assert payload["error"]["kind"] == "claim_conflict"
        return payload

    cli("init")
    return cli


def add_scheduled_post(
    cli, tmp_path, *, at="2099-10-09T10:00:00Z", media="https://media.example/photo.jpg"
):
    caption = tmp_path / "caption.txt"
    caption.write_text("Scheduled kiln firing", encoding="utf-8")
    return cli(
        "queue",
        "add",
        "--tenant",
        "demo",
        "--channel",
        "instagram",
        "--kind",
        "photo",
        "--media",
        media,
        "--caption-file",
        caption,
        "--at",
        at,
    )["post"]


def test_held_due_post_publishes_only_after_approval(queue_cli, tmp_path):
    cli = queue_cli
    post_id = add_scheduled_post(cli, tmp_path, at="2000-01-01T10:00:00Z")["id"]
    cli("queue", "approve", post_id)
    held = cli("queue", "unapprove", post_id, "--hold")["post"]
    assert held["status"] == "draft"
    assert held["held"] is True
    run = cli("run", "--due", "--dry-run")
    assert run["due"] == run["published"] == 0
    assert cli("queue", "show", post_id)["post"]["attempts"] == []

    cli("queue", "approve", post_id)
    assert cli("run", "--due", "--dry-run")["published"] == 1
    published = cli("queue", "show", post_id)["post"]
    assert published["status"] == "published"
    assert published["held"] is False
    assert len(published["receipts"]) == 1


@pytest.mark.parametrize("draft", [False, True], ids=["approved", "draft"])
def test_retry_failed_due_post_respects_approval(queue_cli, tmp_path, draft):
    cli = queue_cli
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"fixture")
    post_id = add_scheduled_post(cli, tmp_path, at="2000-01-01T10:00:00Z", media=media)[
        "id"
    ]
    cli("queue", "approve", post_id)
    # Local media without a hosting hook fails before any provider request.
    cli("run", "--due", "--dry-run", status=3)
    failed = cli("queue", "show", post_id)["post"]
    assert failed["status"] == "failed"
    assert len(failed["attempts"]) == 1
    assert failed["receipts"] == []

    config = tmp_path / "tenants" / "demo" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "host_cmd = []",
            "host_cmd = "
            + json.dumps(
                [
                    sys.executable,
                    "-c",
                    "print('https://media.example/photo.jpg')",
                    "{path}",
                ]
            ),
        ),
        encoding="utf-8",
    )
    retried = cli("queue", "retry", post_id, *(["--draft"] if draft else []))["post"]
    assert retried["status"] == ("draft" if draft else "approved")
    assert retried["at"] == failed["at"]
    assert retried["error"] is None
    if draft:
        run = cli("run", "--due", "--dry-run")
        assert run["due"] == run["published"] == 0
        waiting = cli("queue", "show", post_id)["post"]
        assert waiting["attempts"] == failed["attempts"]
        assert waiting["receipts"] == []
        cli("queue", "approve", post_id)
    assert cli("run", "--due", "--dry-run")["published"] == 1
    published = cli("queue", "show", post_id)["post"]
    assert published["status"] == "published"
    assert len(published["attempts"]) == 2
    assert len(published["receipts"]) == 1


def test_unapprove_hold_timing_and_list_flag(queue_cli, tmp_path):
    cli = queue_cli
    initial = add_scheduled_post(cli, tmp_path)
    post_id = initial["id"]
    cli("queue", "approve", post_id)
    draft = cli("queue", "unapprove", post_id)["post"]
    assert draft["status"] == "draft"
    assert draft["held"] is False
    assert draft["at"] == initial["at"]
    cli(
        "queue",
        "unapprove",
        post_id,
        "--hold",
        "--at",
        "2099-10-11T10:00:00Z",
        status=5,
    )
    assert cli("queue", "show", post_id)["post"] == {
        **draft,
        "attempts": [],
        "receipts": [],
    }

    cli("queue", "approve", post_id)
    held = cli(
        "queue", "unapprove", post_id, "--hold", "--at", "2099-10-10T10:00:00+02:00"
    )["post"]
    assert held["status"] == "draft"
    assert held["held"] is True
    assert held["at"].startswith("2099-10-10T08:00:00")
    assert cli("queue", "list")["posts"] == [held]
    approved = cli("queue", "approve", post_id)["post"]
    assert approved["held"] is False
    assert approved["at"] == held["at"]

    cli("queue", "unapprove", post_id, "--hold", "--at", "invalid", status=2)
    assert cli("queue", "list")["posts"] == [approved]
    described = cli("describe")
    assert {"unapprove", "reschedule"} <= set(described["commands"]["queue"])
    assert "--draft" in described["queue_usage"]["retry"]
    assert described["version"] == "0.3.0"


def test_reschedule_preserves_status_and_hold_and_refuses_terminal_posts(
    queue_cli, tmp_path
):
    cli = queue_cli
    post_id = add_scheduled_post(cli, tmp_path)["id"]
    for state in ("draft", "held", "approved"):
        if state == "held":
            cli("queue", "approve", post_id)
            cli("queue", "unapprove", post_id, "--hold")
        elif state == "approved":
            cli("queue", "approve", post_id)
        before = cli("queue", "show", post_id)["post"]
        after = cli(
            "queue", "reschedule", post_id, "--at", "2000-01-01T12:00:00+02:00"
        )["post"]
        assert after["at"].startswith("2000-01-01T10:00:00")
        assert after["status"] == before["status"]
        assert after["held"] is before["held"]
        # Move it back so each step exercises a real change in time.
        cli("queue", "reschedule", post_id, "--at", "2099-10-09T10:00:00Z")

    cli("queue", "reschedule", post_id, status=2)
    cli("queue", "reschedule", post_id, "--at", "invalid", status=2)
    cli("queue", "reschedule", post_id, "--at", "2000-01-01T10:00:00Z")
    assert cli("run", "--due", "--dry-run")["published"] == 1
    cancelled_id = add_scheduled_post(cli, tmp_path)["id"]
    cli("queue", "cancel", cancelled_id)
    for terminal_id in (post_id, cancelled_id):
        before = cli("queue", "show", terminal_id)["post"]
        cli(
            "queue", "reschedule", terminal_id, "--at", "2099-10-11T10:00:00Z", status=5
        )
        cli(
            "queue",
            "unapprove",
            terminal_id,
            "--hold",
            "--at",
            "2099-10-11T10:00:00Z",
            status=5,
        )
        assert cli("queue", "show", terminal_id)["post"] == before


def test_legacy_store_migrates_without_losing_scheduled_posts(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "postdesk-0.2.3.sql"
    with sqlite3.connect(tmp_path / "postdesk.sqlite3") as connection:
        connection.executescript(fixture.read_text(encoding="utf-8"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "postdesk",
            "--store",
            str(tmp_path),
            "queue",
            "list",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    posts = json.loads(result.stdout)["posts"]
    assert len(posts) == 1
    legacy = posts[0]
    assert legacy["id"] == 1
    assert legacy["external_ref"] == "legacy-post"
    assert legacy["caption"] == "Existing scheduled post"
    assert legacy["status"] == "approved"
    assert legacy["at"].startswith("2099-10-09T10:00:00")
    assert legacy["held"] is False


def test_queue_changes_refuse_a_post_owned_by_the_publisher(queue_cli, tmp_path):
    cli = queue_cli
    media = tmp_path / "photo.jpg"
    media.write_bytes(b"fixture")
    post_id = add_scheduled_post(cli, tmp_path, at="2000-01-01T10:00:00Z", media=media)[
        "id"
    ]
    # A real media-host hook attempts queue edits after the publisher claims the row.
    hook = tmp_path / "host.py"
    hook.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "store = Path(sys.argv[1]).parent\n"
        "base = [sys.executable, '-m', 'postdesk', '--store', str(store), 'queue']\n"
        f"post_id = {str(post_id)!r}\n"
        "def invoke(*args):\n"
        "    result = subprocess.run([*base, *args, '--json'], capture_output=True, text=True, timeout=10)\n"
        "    return {'status': result.returncode, 'payload': json.loads(result.stdout or result.stderr)}\n"
        "results = [invoke('show', post_id),\n"
        "           invoke('unapprove', post_id, '--hold', '--at', '2099-10-11T10:00:00Z'),\n"
        "           invoke('reschedule', post_id, '--at', '2099-10-11T10:00:00Z'),\n"
        "           invoke('cancel', post_id),\n"
        "           invoke('show', post_id)]\n"
        "(store / 'claimed-results.json').write_text(json.dumps(results))\n"
        "print('https://media.example/photo.jpg')\n",
        encoding="utf-8",
    )
    config = tmp_path / "tenants" / "demo" / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "host_cmd = []",
            f"host_cmd = {json.dumps([sys.executable, str(hook), '{path}'])}",
        ),
        encoding="utf-8",
    )
    cli("queue", "approve", post_id)
    assert cli("run", "--due", "--dry-run")["published"] == 1
    before, unapprove, reschedule, cancel, after = json.loads(
        (tmp_path / "claimed-results.json").read_text()
    )
    assert before["payload"]["post"]["claimed"] is True
    assert before["payload"]["post"]["status"] == "publishing"
    for refused in (unapprove, reschedule, cancel):
        assert refused["status"] == 5
        assert refused["payload"]["error"]["kind"] == "claim_conflict"
    assert after == before


def test_reschedule_and_publish_only_the_approved_post(tmp_path):
    def cli(*args, ok=True):
        result = subprocess.run([sys.executable, "-m", "postdesk", "--store", str(tmp_path), *args, "--json"], capture_output=True, text=True)
        assert (result.returncode == 0) == ok, result.stdout + result.stderr
        return json.loads(result.stdout or result.stderr)

    cli("init")
    caption = tmp_path / "caption.txt"
    caption.write_text("Kiln firing. #WoodFiring", encoding="utf-8")
    def add(ref):
        return cli("queue", "add", "--tenant", "demo", "--channel", "instagram", "--kind", "photo", "--media", "https://media.example/photo.jpg", "--caption-file", str(caption), "--at", "2099-10-09T12:00:00+02:00", "--external-ref", ref)["post"]["id"]

    chosen, other = add("chosen"), add("other")
    cli("queue", "approve", str(chosen))
    cli("queue", "approve", str(other), "--now")
    changed = cli("queue", "approve", str(chosen), "--at", "2099-10-10T10:00:00+02:00")["post"]
    assert changed["at"].startswith("2099-10-10T08:00:00")
    cli("queue", "approve", str(chosen), "--now")
    result = cli("run", "--due", "--post", str(chosen), "--dry-run")
    assert result["published"] == 1
    assert cli("queue", "show", str(other))["post"]["status"] == "approved"
    repeated = cli("queue", "approve", str(chosen), "--now")["post"]
    assert repeated["status"] == "published"
    assert len(cli("queue", "show", str(chosen))["post"]["receipts"]) == 1
    cli("queue", "approve", str(other), "--now", "--at", "2099-10-10T10:00:00Z", ok=False)
