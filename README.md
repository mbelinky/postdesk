# Postdesk

> Early release. The queue, dry-run, receipts, and fixture-backed tests work. Live publishing still requires your own Meta app, credentials, media hosting, and deployment checks.

[![Tests](https://github.com/mbelinky/postdesk/actions/workflows/test.yml/badge.svg)](https://github.com/mbelinky/postdesk/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-6b6257.svg)](LICENSE)

Postdesk is a local-first publishing desk for Instagram, Facebook, and YouTube. It queues, approves, schedules, publishes, and records what happened in a SQLite database.

Publishing APIs fail at awkward moments. A request can time out after the network has accepted it, which makes an automatic retry dangerous. Postdesk records every attempt before it contacts a network. If the result is uncertain, it stops and asks the network what happened instead of posting twice.

## What it does

- Queues drafts for Instagram, Facebook, and YouTube.
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

[channels.youtube]
channel_id = "replace-me"
token_env = "POSTDESK_YOUTUBE_TOKEN"
client_id_env = "POSTDESK_YOUTUBE_CLIENT_ID"
client_secret_env = "POSTDESK_YOUTUBE_CLIENT_SECRET"
privacy = "public"
# optional: expected_handle = "@yourchannel", playlist_id = "PL...", category_id = "22", default_language = "en"
```

`host_cmd` must return one public HTTP or HTTPS URL for local media. `notify_cmd` receives failures and uncertain outcomes. Both are argument arrays, so Postdesk does not invoke a shell.

An Instagram channel talks to `graph.instagram.com` by default, which takes an Instagram-login token. A Meta system-user or Facebook-login token works only on `graph.facebook.com`; set `graph_host = "graph.facebook.com"` on that channel. The account check then reads the Instagram user node instead of `/me`.

### YouTube

YouTube takes the video bytes directly, so a local path uploads without `host_cmd`; an HTTP(S) URL is downloaded first. One Google Cloud OAuth client serves every tenant, and each tenant binds its own channel:

1. In Google Cloud Console enable "YouTube Data API v3", set the OAuth consent screen to External with publishing status "In production" (in "Testing", Google revokes the login after 7 days), and create an OAuth client of type "Desktop app".
2. Export the client id and secret in the two `client_*_env` variables, then run `postdesk auth youtube` on a machine with a browser and sign in as the channel owner. It prints the `channel_id` for `config.toml` and the refresh token to store in `token_env`.
3. Until Google approves the project's YouTube API compliance audit, every upload is forced to private whatever `privacy` says.

`--title` sets the video title (100 characters); without it the caption's first line, minus hashtags, is used and the caption becomes the description. A vertical video under three minutes is listed as a Short automatically. The default quota allows about six uploads a day.

## Supported posts

| Network | Kinds | Notes |
| --- | --- | --- |
| Instagram | photo, carousel, reel, story | First comments, collaborators, quota checks, and raw media insights when Meta returns them |
| Facebook | text, link, photo, album, video | Caption editing, deletion, video polling, and raw media insights |
| YouTube | video | Title plus description, first comment, playlist add, processing polling, description editing, deletion, and view/like/comment counts |

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
postdesk queue         add|list|show|retry|cancel|preview
postdesk queue         approve ID [--now|--at ISO]
postdesk queue         unapprove ID [--hold] [--at ISO]
postdesk queue         reschedule ID --at ISO
postdesk run           --due [--post ID] [--dry-run]
postdesk reconcile
postdesk publish       POST_FLAGS [--dry-run]
postdesk edit          --receipt ID --caption-file FILE
postdesk delete        --receipt ID
postdesk insights      pull --tenant NAME [--since ISO]
postdesk capabilities  --tenant NAME
postdesk auth          youtube [--no-browser]
postdesk describe
```

`queue approve ID --now` makes an approved row due now; `--at ISO` changes its time. Published rows stay published. Claimed or uncertain rows cannot be rescheduled. `run --due --post ID` publishes only that row.

`queue unapprove ID` returns an unclaimed approved post to draft. Add `--hold` to mark it held and `--at ISO` to change its time in the same step. Drafts, including held posts, stay outside the publish path. Approving a draft clears its hold and keeps its time unless `--at` or `--now` is given.

`queue reschedule ID --at ISO` changes the time of an unclaimed draft or approved post, keeping its status and hold flag. Other states refuse the change with exit status 5. Queue JSON includes `held` as a boolean.

Every command supports `--json`. Invalid input exits with status 2, configuration errors with 3, network errors with 4, and claim conflicts with 5.

## Development

```sh
python -m pytest -q
```

The tests block socket connections and replace network transports with recorded fixtures. They cover every advertised post kind, dry-run transcripts, parallel claims, hooks, attempts, receipts, and reconciliation.

## License

Postdesk is available under the [MIT License](LICENSE).
