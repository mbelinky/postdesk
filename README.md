# postdesk

`postdesk` is a standalone social publishing desk. It queues, approves, schedules, publishes, reconciles uncertain attempts, stores receipts, and pulls raw network metrics for tenant-configured Instagram and Facebook channels.

The core is network-neutral. Each network lives behind one driver interface. Tenant configuration names environment variables that hold credentials; configuration files never contain credential values.

## Quickstart

Python 3.12 or newer is required.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

STORE_DIR="$(mktemp -d)"
.venv/bin/postdesk init --store "$STORE_DIR" --json
.venv/bin/postdesk describe --json
.venv/bin/postdesk capabilities --store "$STORE_DIR" --tenant demo --json
```

`init` creates `postdesk.sqlite3` and copies the `demo` tenant into `tenants/demo/config.toml`. The demo IDs are inert placeholders. Dry-run does not require token environment variables and never makes HTTP requests.

Queue and publish an Instagram photo without contacting Instagram:

```bash
printf '%s\n' 'Demo caption' > "$STORE_DIR/caption.txt"

.venv/bin/postdesk queue --store "$STORE_DIR" add \
  --tenant demo \
  --channel instagram \
  --kind photo \
  --media https://media.example/photo.jpg \
  --caption-file "$STORE_DIR/caption.txt" \
  --external-ref demo-instagram-photo \
  --json

.venv/bin/postdesk queue --store "$STORE_DIR" approve 1 --json
.venv/bin/postdesk run --store "$STORE_DIR" --due --dry-run --json
.venv/bin/postdesk queue --store "$STORE_DIR" show 1 --json
.venv/bin/postdesk reconcile --store "$STORE_DIR" --json
```

Publish an immediate Facebook link in the same store:

```bash
.venv/bin/postdesk publish --store "$STORE_DIR" \
  --tenant demo \
  --channel facebook \
  --kind link \
  --caption-file "$STORE_DIR/caption.txt" \
  --link https://example.com \
  --external-ref demo-facebook-link \
  --dry-run \
  --json
```

Every dry-run result includes the redacted API calls that a live run would make. Dry-run follows the real local lifecycle and records a receipt, so queue, lease, attempt, and receipt behavior can be tested end to end.

## Tenant configuration

Tenant files live at `tenants/<name>/config.toml` next to the store. Set `POSTDESK_TENANTS_DIR` to use another configuration root.

```toml
[hooks]
host_cmd = ["media-host", "--input", "{path}"]
notify_cmd = ["notify-operator", "--message", "{message}", "--attachment", "{file}"]
timeout_seconds = 30

[channels.instagram]
ig_user_id = "replace-me"
token_env = "POSTDESK_EXAMPLE_INSTAGRAM_TOKEN"

[channels.facebook]
page_id = "replace-me"
token_env = "POSTDESK_EXAMPLE_FACEBOOK_TOKEN"
```

`host_cmd` and `notify_cmd` are argument arrays, not shell commands. Placeholders must be standalone arguments. The host hook must print one public HTTP(S) URL. A host failure stops publishing. A notify failure is stored on the post and never rolls back a publish or reconciliation decision.

Live publishing resolves the token from `token_env`. Dry-run substitutes a redacted token marker. Drivers receive resolved credentials and hooks through the core context; they do not read files or environment variables.

## Command surface

```text
postdesk init      --store DIR
postdesk tenant    add|list|show NAME
postdesk queue     add|list|show|approve|cancel|preview
postdesk run       --due [--dry-run]
postdesk reconcile
postdesk publish   POST_FLAGS [--dry-run]
postdesk edit      --receipt ID --caption-file FILE
postdesk delete    --receipt ID
postdesk insights  pull --tenant NAME [--since ISO]
postdesk capabilities --tenant NAME
postdesk describe
```

Use `--store` or `POSTDESK_STORE` for the SQLite file or its containing directory. State-changing publish commands validate the selected kind before a row is queued. `external_ref` is unique per tenant; repeating it returns the existing post instead of creating another delivery.

All commands support `--json`. Structured errors have this shape:

```json
{"ok": false, "error": {"kind": "invalid_input", "detail": "...", "path": "..."}}
```

Exit statuses are 2 for invalid input, 3 for configuration or credentials, 4 for network or API failures, and 5 for claim conflicts.

## At-most-once protocol

The SQLite store is the authority for posts, publish attempts, receipts, and metrics. A due worker atomically takes a lease. Before the first driver call, the core commits a publish-attempt row. Before each request that can create remote state, the driver checkpoints that the request is starting; it checkpoints every returned container, child, video, and post ID immediately.

An expired or uncertain attempt is never blindly retried. `reconcile` asks the driver to look it up:

- `found` stores the receipt and closes the post as published.
- `not_found` closes the attempt and releases the post for scheduling.
- `unknown` keeps the post `in_doubt`, records the reason, and notifies the operator.

Async work returns a pending handle. The core owns polling limits and persists the handle between polls. Instagram reels, video carousels, and stories use container status polling. Facebook video uses video status polling.

## Capability matrices

The live matrix is available from `postdesk capabilities --tenant demo --json`. Validation uses this same data.

### Instagram

| Kind | Media | Types | Caption | Async |
| --- | ---: | --- | --- | --- |
| `photo` | 1 | image | required | yes |
| `carousel` | 2..10 | image or video | required | yes |
| `reel` | 1 | video | required | yes |
| `story` | 1 | image or video | forbidden | yes |

Instagram supports first comments, collaborators, publishing-quota checks, and raw media insights for views, reach, likes, comments, and saves when returned by the API. Caption editing, deleting, location, music, and pinning are not advertised.

### Facebook

| Kind | Media | Types | Caption | Link | Async |
| --- | ---: | --- | --- | --- | --- |
| `text` | 0 | none | required | forbidden | no |
| `link` | 0 | none | optional | required | no |
| `photo` | 1 | image | optional | forbidden | no |
| `album` | 2..10 | image | optional | forbidden | no |
| `video` | 1 | video | optional | forbidden | yes |

Facebook supports caption editing and deletion. It does not advertise first comments or collaborators. The queue is the only scheduler; the driver never sends `scheduled_publish_time`. Metrics are stored under the exact names and values returned by Facebook, with no cross-network normalization.

## Development and proof

The test suite blocks socket connections and replaces every driver transport with recorded fixtures. It covers every advertised kind, API error mapping, dry-run call transcripts, hook success and failure, capability gating, parallel claims, durable attempts, receipts, and reconciliation.

```bash
.venv/bin/python -m pytest -q
```

`docs/reference/` contains sanitized prior art used to port request flows. Runtime code never imports it, and `MANIFEST.in` excludes it from source distributions.

## Open items before publishing

- Choose a license.
- Run the pending Facebook live smoke test when a Page token is available.
- Any Instagram cutover for an existing installation is outside this repository.
