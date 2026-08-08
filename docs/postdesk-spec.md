# postdesk — social publishing desk · spec v1

A standalone, tenant-neutral publishing tool: queue → approve → schedule →
publish (at most once) → receipt → insights. Networks are drivers behind one
interface. Built to be published as an independent open tool; it must contain
zero traces of any particular business.

## Non-negotiable design rules

1. **One product, driver modules.** The queue, approval, scheduling, claims,
   receipts and metrics are network-agnostic core. Instagram and Facebook are
   drivers implementing the same small interface. A future network is one new
   driver file.
2. **Tenant is a string plus a config folder.** No organizer tables, no brand
   names in code. `tenants/<name>/config.toml` holds channel credentials (as
   environment-variable references, never literal secrets), page/account ids,
   and hook commands. Ships with one example tenant named `demo`.
3. **Seams are argv templates and files**, same contracts as the sibling
   tools:
   - **Media hosting hook** (Instagram requires a publicly fetchable URL):
     a post's media is either already a public URL, or a local path that the
     tenant's `host_cmd` turns into one — argv template, `{path}` substituted
     as a single argument, stdout is the URL, non-zero exit fails the publish.
     The tool ships no hosting of its own.
   - **Notify hook**: `notify_cmd` argv template with `{message}` and
     optional `{file}`; executed without a shell; timeout configurable;
     non-zero is logged on the item, never rolls back state.
4. **Own store.** One SQLite file (path via `--store` / `POSTDESK_STORE`),
   SQLAlchemy models: posts, receipts, metrics. Every row carries `tenant`.
   `external_ref` gives callers idempotency.
5. **At most once.** Claim-before-publish with a claim token; a crash after
   claim leaves a claimed row that `reconcile` resolves against the network
   (receipt exists remotely → record it; nothing remote → release the claim).
   Never auto-retry a publish that may have landed.
6. **JSON everywhere.** Every command takes/returns JSON-friendly output with
   `--json`; errors are structured (`ok:false, error:{kind, detail, path}`)
   with distinct exit codes for: invalid input (2), config/credential
   problems (3), network/API errors (4), claim conflicts (5).
7. **No live network calls in the test suite.** Drivers are tested against
   recorded request/response fixtures; a `--dry-run` on publish prints the
   exact API calls that would be made.

## CLI surface (v1)

```
postdesk init      --store DIR
postdesk tenant    add|list|show <name>
postdesk queue     add --tenant T --channel instagram|facebook --kind KIND \
                       --media PATH_OR_URL [--media ... repeated for albums/carousels] \
                       --caption-file F [--first-comment-file F] [--collaborator U] \
                       [--link URL] [--at ISO] [--external-ref R] --json
postdesk queue     list|show|approve|cancel|preview [--status ...] --json
postdesk run       --due --json          # the timer target
postdesk reconcile --json
postdesk publish   ... (same flags as queue add, immediate, no approval gate;
                        intended for operator use and tests)
postdesk edit      --receipt ID --caption-file F --json   # capability-gated
postdesk delete    --receipt ID --json                    # capability-gated
postdesk insights  pull --tenant T [--since ISO] --json
postdesk capabilities --tenant T --json   # per-channel matrix, honest
postdesk describe  --json                 # tool self-description for agents
```

`queue add` validates against the driver's capabilities immediately (an
Instagram `link` post or a Facebook `story` is rejected at add time with the
capability named, not at publish time).

## Driver interface

```python
class Driver(Protocol):
    id: str
    def capabilities(self) -> Capabilities        # kinds, features, limits
    def validate(self, post: Post) -> list[str]   # human-readable problems
    def publish(self, post: Post, ctx: Ctx) -> Receipt
    def edit(self, receipt: Receipt, changes: Changes, ctx: Ctx) -> Receipt   # optional
    def delete(self, receipt: Receipt, ctx: Ctx) -> None                      # optional
    def insights(self, receipts: list[Receipt], ctx: Ctx) -> list[Metric]
```

`Ctx` provides resolved credentials, the media-URL resolver (applies the
host hook), a logger, and the dry-run flag. Drivers never read config files
or environment directly.

## Instagram driver (port of a proven flow)

Container flow against `graph.instagram.com`: photo, carousel (child
containers → carousel container), reel (video container + status poll),
story; `first_comment` posted after publish; `collaborators` on container
creation; publishing-quota check exposed in capabilities; media insights
(views, reach, likes, comments, saves as available). Auth per tenant:
`ig_user_id` + token env ref. Known impossibilities stay impossible and are
absent from capabilities: caption edit, location, music, pinning.

Reference for exact call shapes: the operator supplies a mirror of a working
implementation in `docs/reference/` (vendored, to be treated as prior art —
port the flows, do not import the code).

## Facebook driver (new)

Against `graph.facebook.com` with a Page access token (`page_id` + token env
ref per tenant):
- `link`/text post: `POST /{page}/feed` (message, link).
- `photo`: `POST /{page}/photos`.
- `album` (multi-photo): unpublished photo uploads → `POST /{page}/feed`
  with `attached_media`.
- `video`: `POST /{page}/videos` (resumable not required in v1; size-guard
  in validate).
- `edit`: `POST /{post-id}` (message) — Facebook allows it; capability
  advertised.
- `delete`: `DELETE /{post-id}`.
- insights: post impressions, reach, clicks, reactions (whatever the token
  scope returns; degrade gracefully and record what was obtained).
The queue owns scheduling; Facebook's native `scheduled_publish_time` is NOT
used (one scheduler, one truth).

## Tests and proof

- Queue lifecycle on a temp store with a fake driver: add → approve → run
  claims exactly once under parallel `run` invocations → receipt → reconcile
  idempotence.
- Driver contract tests against fixtures for every kind listed above,
  including error mapping (API error → exit 4 with detail) and dry-run call
  transcripts.
- Hook tests: host_cmd success/failure/timeout; notify never fatal.
- Capability gating at `queue add`.
- Proof block: full `pytest -q`; `describe`; a scripted end-to-end lifecycle
  in a temp store with both drivers in dry-run; capabilities matrices for a
  demo tenant with both channels.

## Out of scope v1

Conversations, DMs, keyword hooks (that is a bot's job); ads creation;
boosting; TikTok/Pinterest; any web UI; media hosting; approval transport
(how an approval reaches the CLI is the installer's business).

## Open items recorded in README

License choice before publishing; Facebook live smoke test pends a page
token (expected the day after build); Instagram live cutover of any existing
installation is explicitly not this repo's concern.
