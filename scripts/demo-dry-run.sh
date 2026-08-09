#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="$repo_dir/.venv/bin/python"
demo_dir="$(mktemp -d)"
trap 'rm -rf "$demo_dir"' EXIT

printf '%s\n' 'Dry-run proof caption' > "$demo_dir/caption.txt"

"$python_bin" -m postdesk init --store "$demo_dir" --json > "$demo_dir/init.json"

"$python_bin" -m postdesk queue add \
  --store "$demo_dir" \
  --tenant demo \
  --channel instagram \
  --kind reel \
  --media https://media.example/proof-reel.mp4 \
  --caption-file "$demo_dir/caption.txt" \
  --external-ref proof-instagram-reel \
  --at 2020-01-01T00:00:00Z \
  --json > "$demo_dir/instagram-add.json"

"$python_bin" -m postdesk queue approve \
  --store "$demo_dir" 1 --json > "$demo_dir/instagram-approve.json"

"$python_bin" -m postdesk queue add \
  --store "$demo_dir" \
  --tenant demo \
  --channel facebook \
  --kind video \
  --media https://media.example/proof-video.mp4 \
  --caption-file "$demo_dir/caption.txt" \
  --external-ref proof-facebook-video \
  --at 2020-01-01T00:00:00Z \
  --json > "$demo_dir/facebook-add.json"

"$python_bin" -m postdesk queue approve \
  --store "$demo_dir" 2 --json > "$demo_dir/facebook-approve.json"

"$python_bin" -m postdesk run \
  --store "$demo_dir" --due --dry-run --json > "$demo_dir/run.json"

"$python_bin" -m postdesk queue list \
  --store "$demo_dir" --status published --json > "$demo_dir/published.json"

"$python_bin" -m postdesk reconcile \
  --store "$demo_dir" --json > "$demo_dir/reconcile.json"

"$python_bin" -m postdesk capabilities \
  --store "$demo_dir" --tenant demo --json > "$demo_dir/capabilities.json"

"$python_bin" - "$demo_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text(encoding="utf-8"))
run = load("run.json")
published = load("published.json")
reconciled = load("reconcile.json")
capabilities = load("capabilities.json")

summary = {
    "ok": all(
        [
            load("init.json")["ok"],
            load("instagram-add.json")["created"],
            load("instagram-approve.json")["post"]["status"] == "approved",
            load("facebook-add.json")["created"],
            load("facebook-approve.json")["post"]["status"] == "approved",
            run["ok"],
            run["published"] == 2,
            len(published["posts"]) == 2,
            reconciled["checked"] == 0,
            set(capabilities["channels"]) == {"instagram", "facebook"},
        ]
    ),
    "store": "temporary",
    "posts": [
        {
            "id": item["id"],
            "channel": item["channel"],
            "kind": item["kind"],
            "status": item["status"],
        }
        for item in published["posts"]
    ],
    "receipts": [
        {
            "channel": item["receipt"]["channel"],
            "external_id": item["receipt"]["external_id"],
            "api_calls": item["api_calls"],
        }
        for item in run["results"]
    ],
    "reconcile": reconciled,
    "capability_kinds": {
        channel: sorted(matrix["kinds"])
        for channel, matrix in capabilities["channels"].items()
    },
}
print(json.dumps(summary, sort_keys=True))
raise SystemExit(0 if summary["ok"] else 1)
PY
