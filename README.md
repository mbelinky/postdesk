# Postdesk

> Early release. The queue, dry-run, receipts, and fixture-backed tests work. Live publishing still requires your own Meta app, credentials, media hosting, and deployment checks.

[![Tests](https://github.com/mbelinky/postdesk/actions/workflows/test.yml/badge.svg)](https://github.com/mbelinky/postdesk/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-6b6257.svg)](LICENSE)

Postdesk is a local-first publishing desk for Instagram and Facebook. It queues, approves, schedules, publishes, and records what happened in a SQLite database.

Publishing APIs fail at awkward moments. A request can time out after the network has accepted it, which makes an automatic retry dangerous. Postdesk records every attempt before it contacts a network. If the result is uncertain, it stops and asks the network what happened instead of posting twice.

## What it does

- Queues drafts for Instagram and Facebook.
- Requires approval before scheduled posts can run.
- Shows the exact redacted API calls in dry-run mode without contacting Meta.
- Stores attempts, remote identifiers, receipts, and raw network metrics.
- Reconciles uncertain attempts before allowing a retry.
- Keeps credentials in environment variables, outside tenant configuration files.
- Exposes one JSON-friendly command-line interface for scripts and agents.

## Quick start

Postdesk requires Python 3.12 or newer.

```sh
git clone https://github.com/mbelinky/postdesk.git
cd postdesk
python -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
./scripts/demo-dry-run.sh
```

The demo creates a temporary database, queues one Instagram reel and one Facebook video, approves both, and runs the real local lifecycle without making a network request. It deletes the temporary files when it finishes.

## Try one post

```sh
STORE_DIR="$(mktemp -d)"
postdesk init --store "$STORE_DIR" --json

printf '%s\n' 'A caption from Postdesk' > "$STORE_DIR/caption.txt"

postdesk queue add \
  --store "$STORE_DIR" \
  --tenant demo \
  --channel instagram \
  --kind photo \
  --media https://media.example/photo.jpg \
  --caption-file "$STORE_DIR/caption.txt" \
  --external-ref first-demo \
  --json

postdesk queue approve --store "$STORE_DIR" 1 --json
postdesk run --store "$STORE_DIR" --due --dry-run --json
```

Dry-run uses placeholder credentials and follows the same local queue, claim, attempt, and receipt path as a live run.

## Configure a tenant

Tenant files live at `tenants/<name>/config.toml` beside the database. They contain account identifiers and the names of environment variables, never token values.

```toml
[hooks]
host_cmd = ["media-host", "--input", "{path}"]
notify_cmd = ["notify-operator", "--message", "{message}"]
timeout_seconds = 30

[channels.instagram]
ig_user_id = "replace-me"
expected_username = "replace-me"
token_env = "POSTDESK_INSTAGRAM_TOKEN"

[channels.facebook]
page_id = "replace-me"
token_env = "POSTDESK_FACEBOOK_TOKEN"
```

`host_cmd` must return one public HTTP or HTTPS URL for local media. `notify_cmd` receives failures and uncertain outcomes. Both are argument arrays, so Postdesk does not invoke a shell.

## Supported posts

| Network | Kinds | Notes |
| --- | --- | --- |
| Instagram | photo, carousel, reel, story | First comments, collaborators, quota checks, and raw media insights when Meta returns them |
| Facebook | text, link, photo, album, video | Caption editing, deletion, video polling, and raw media insights |

Run `postdesk capabilities --store STORE --tenant NAME --json` to read the exact capability matrix used by validation.

## Failure handling

The SQLite database is the authority for posts and attempts. Before any request that can create a remote post, Postdesk commits an attempt row and checkpoints every returned remote identifier.

An expired or uncertain attempt enters `in_doubt`. `postdesk reconcile` then asks the network for one of three answers:

- `found`: store the receipt and mark the post published.
- `not_found`: close the attempt and release the post for another run.
- `unknown`: keep the post blocked and notify the operator.

Postdesk never blindly retries an uncertain publish.

## Command map

```text
postdesk init          --store DIR
postdesk tenant        add|list|show NAME
postdesk queue         add|list|show|approve|cancel|preview
postdesk run           --due [--dry-run]
postdesk reconcile
postdesk publish       POST_FLAGS [--dry-run]
postdesk edit          --receipt ID --caption-file FILE
postdesk delete        --receipt ID
postdesk insights      pull --tenant NAME [--since ISO]
postdesk capabilities  --tenant NAME
postdesk describe
```

Every command supports `--json`. Invalid input exits with status 2, configuration errors with 3, network errors with 4, and claim conflicts with 5.

## Development

```sh
python -m pytest -q
```

The tests block socket connections and replace network transports with recorded fixtures. They cover every advertised post kind, dry-run transcripts, parallel claims, hooks, attempts, receipts, and reconciliation.

## License

Postdesk is available under the [MIT License](LICENSE).
