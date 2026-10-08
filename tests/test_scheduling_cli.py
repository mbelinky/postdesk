from __future__ import annotations

import json
import subprocess
import sys


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
