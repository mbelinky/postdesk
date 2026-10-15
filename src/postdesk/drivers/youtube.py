"""YouTube driver: resumable upload to the tenant's own channel.

Credentials follow the tenant convention: ``config.toml`` names the channel
and the environment variables, never the values. One Google Cloud OAuth
client (``client_id_env`` / ``client_secret_env``) serves every tenant; each
tenant binds its channel through ``token_env`` (a refresh token obtained once
with ``postdesk auth youtube``) and ``channel_id``.

The attempt protocol maps onto Google's resumable upload: the session URL is
checkpointed before any byte is sent, so a lost response can be reconciled by
asking that session for its final state instead of uploading twice.
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..errors import ApiError, ConfigError
from ..http import HttpResponse, JsonTransport, UrlLibJsonTransport
from ..types import (
    AttemptData,
    Capabilities,
    Ctx,
    Failed,
    Found,
    KindCapability,
    MetricData,
    NotFound,
    Pending,
    PostData,
    ReceiptData,
    Unknown,
)
from .common import common_validation

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE_URL = "https://www.googleapis.com/upload/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
TITLE_MAX_CHARS = 100
DESCRIPTION_MAX_CHARS = 5000
PRIVACY_STATUSES = ("private", "unlisted", "public")
DEFAULT_CLIENT_ID_ENV = "POSTDESK_YOUTUBE_CLIENT_ID"
DEFAULT_CLIENT_SECRET_ENV = "POSTDESK_YOUTUBE_CLIENT_SECRET"
METRICS = ("views", "likes", "comments")
RECENT_UPLOADS_TO_SCAN = 10


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def title_for(post: PostData) -> str:
    """The explicit title, else the caption's first line without hashtags."""
    explicit = " ".join(str(post.title or "").split())
    if explicit:
        return explicit
    for line in str(post.caption or "").splitlines():
        words = [word for word in line.split() if not word.startswith("#")]
        candidate = " ".join(words).strip(" -–—:|")
        if candidate:
            return candidate[: TITLE_MAX_CHARS - 1].rstrip() + "…" if len(candidate) > TITLE_MAX_CHARS else candidate
    return ""


class YouTubeDriver:
    id = "youtube"

    def __init__(self, transport: JsonTransport | None = None, *, chunk_bytes: int = UPLOAD_CHUNK_BYTES) -> None:
        self.transport = transport or UrlLibJsonTransport()
        self.chunk_bytes = chunk_bytes
        self._tokens: dict[str, tuple[str, float]] = {}  # refresh token -> (access token, expires at)

    # Capabilities --------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kinds={"video": KindCapability(1, 1, ("video",), "optional", async_publish=True)},
            features={
                "first_comment": True,
                "collaborators": False,
                "edit": True,
                "delete": True,
                "native_scheduling": False,
            },
            metric_names=METRICS,
        )

    def validate(self, post: PostData) -> list[str]:
        capability = self.capabilities().kinds.get(post.kind)
        if capability is None:
            return [f"youtube.kind {post.kind!r} is not supported."]
        problems = common_validation(post, capability)
        if post.collaborators:
            problems.append("youtube.collaborators is not supported.")
        if post.link:
            problems.append("youtube.link is not supported; put links in the caption.")
        title = title_for(post)
        if not title:
            problems.append("youtube.video.title is required: pass --title or a caption whose first line is the title.")
        elif len(title) > TITLE_MAX_CHARS:
            problems.append(f"youtube.video.title must be at most {TITLE_MAX_CHARS} characters.")
        if any(char in title for char in "<>"):
            problems.append("youtube.video.title may not contain < or >.")
        caption = post.caption or ""
        if len(caption) > DESCRIPTION_MAX_CHARS:
            problems.append(f"youtube.video.caption must be at most {DESCRIPTION_MAX_CHARS} characters.")
        if any(char in caption for char in "<>"):
            problems.append("youtube.video.caption may not contain < or >.")
        return problems

    # Credentials ---------------------------------------------------------

    def _settings(self, ctx: Ctx) -> dict[str, str]:
        credentials = ctx.credentials
        channel_id = str(credentials.get("channel_id") or "").strip()
        if not channel_id:
            raise ConfigError("channel_id is required.", path="channels.youtube.channel_id")
        client_id_env = str(credentials.get("client_id_env") or DEFAULT_CLIENT_ID_ENV).strip()
        client_secret_env = str(credentials.get("client_secret_env") or DEFAULT_CLIENT_SECRET_ENV).strip()
        client_id = os.environ.get(client_id_env, "").strip()
        client_secret = os.environ.get(client_secret_env, "").strip()
        if not ctx.dry_run:
            for name, value, key in ((client_id_env, client_id, "client_id_env"), (client_secret_env, client_secret, "client_secret_env")):
                if not value:
                    raise ConfigError(f"Environment variable {name} is not set.", path=f"channels.youtube.{key}")
        privacy = str(credentials.get("privacy") or "public").strip().lower()
        if privacy not in PRIVACY_STATUSES:
            raise ConfigError(f"privacy must be one of {', '.join(PRIVACY_STATUSES)}.", path="channels.youtube.privacy")
        return {
            "channel_id": channel_id,
            "expected_handle": str(credentials.get("expected_handle") or "").strip().lstrip("@"),
            "client_id": client_id or f"<{client_id_env}>",
            "client_secret": client_secret or f"<{client_secret_env}>",
            "refresh_token": credentials["token"],
            "privacy": privacy,
            "playlist_id": str(credentials.get("playlist_id") or "").strip(),
            "category_id": str(credentials.get("category_id") or "22").strip(),
            "default_language": str(credentials.get("default_language") or "").strip(),
        }

    def _access_token(self, settings: dict[str, str], ctx: Ctx) -> str:
        cached = self._tokens.get(settings["refresh_token"])
        if cached and time.monotonic() < cached[1] - 60:
            return cached[0]
        response = self.transport.send(
            "POST",
            TOKEN_URL,
            headers={},
            form={
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "refresh_token": settings["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=ctx.timeout_seconds,
        )
        token = str(response.payload.get("access_token") or "").strip()
        if not token:
            raise ConfigError("Google returned no access token; run `postdesk auth youtube` again.", path="channels.youtube.token_env")
        self._tokens[settings["refresh_token"]] = (token, time.monotonic() + float(response.payload.get("expires_in") or 3600))
        return token

    def _api(self, method: str, path: str, token: str, ctx: Ctx, *, query: dict | None = None, body: dict | None = None) -> dict[str, Any]:
        response = self.transport.send(
            method,
            f"{API_BASE_URL}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            query=query,
            json_body=body,
            timeout=ctx.timeout_seconds,
        )
        return response.payload

    def _verify_channel(self, token: str, settings: dict[str, str], ctx: Ctx) -> dict[str, Any]:
        payload = self._api("GET", "channels", token, ctx, query={"part": "id,snippet,contentDetails", "mine": "true"})
        items = payload.get("items") or []
        actual = items[0] if items and isinstance(items[0], dict) else {}
        actual_id = str(actual.get("id") or "").strip()
        snippet = actual.get("snippet") if isinstance(actual.get("snippet"), dict) else {}
        actual_handle = str(snippet.get("customUrl") or "").strip().lstrip("@")
        if actual_id != settings["channel_id"]:
            raise ConfigError(
                f"YouTube credential belongs to channel {actual_id or 'unknown'} (@{actual_handle or 'unknown'}), expected {settings['channel_id']}.",
                path="channels.youtube.channel_id",
            )
        expected_handle = settings["expected_handle"]
        if expected_handle and actual_handle and actual_handle.casefold() != expected_handle.casefold():
            raise ConfigError(
                f"YouTube credential belongs to @{actual_handle}, expected @{expected_handle}.",
                path="channels.youtube.expected_handle",
            )
        return actual

    # Media ---------------------------------------------------------------

    def _local_file(self, value: str, ctx: Ctx) -> tuple[Path, bool]:
        """Return (path, is_temporary). Local paths are read in place; URLs are downloaded."""
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            suffix = Path(parsed.path).suffix or ".mp4"
            handle = tempfile.NamedTemporaryFile(prefix="postdesk-youtube-", suffix=suffix, delete=False)
            handle.close()
            destination = Path(handle.name)
            self.transport.fetch_file(value, destination, timeout=max(ctx.timeout_seconds, 600))
            return destination, True
        if parsed.scheme:
            raise ConfigError("Media must be a local path or HTTP(S) URL.", path="media")
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ConfigError("Local media file does not exist.", path=str(path))
        return path, False

    # Metadata ------------------------------------------------------------

    def _metadata(self, post: PostData, settings: dict[str, str]) -> dict[str, Any]:
        snippet: dict[str, Any] = {
            "title": title_for(post),
            "description": (post.caption or "").strip(),
            "categoryId": settings["category_id"],
        }
        if settings["default_language"]:
            snippet["defaultLanguage"] = settings["default_language"]
        return {"snippet": snippet, "status": {"privacyStatus": settings["privacy"], "selfDeclaredMadeForKids": False}}

    def _plan(self, post: PostData, settings: dict[str, str], media: str) -> list[dict[str, Any]]:
        auth = {"Authorization": "Bearer <access_token>"}
        calls = [
            {"method": "POST", "url": TOKEN_URL, "parameters": {"grant_type": "refresh_token", "refresh_token": "<token>"}, "result": "access_token"},
            {"method": "GET", "url": f"{API_BASE_URL}/channels", "parameters": {"part": "id,snippet,contentDetails", "mine": "true"}, "headers": auth, "guard": f"expected channel {settings['channel_id']}"},
            {"method": "POST", "url": f"{UPLOAD_BASE_URL}/videos", "parameters": {"uploadType": "resumable", "part": "snippet,status", "body": self._metadata(post, settings)}, "headers": auth, "result": "upload_session"},
            {"method": "PUT", "url": "{upload_session}", "parameters": {"file": media, "chunk_bytes": self.chunk_bytes}, "headers": auth, "result": "video_id"},
        ]
        if settings["playlist_id"]:
            calls.append({"method": "POST", "url": f"{API_BASE_URL}/playlistItems", "parameters": {"part": "snippet", "playlistId": settings["playlist_id"], "videoId": "{video_id}"}, "headers": auth, "result": "playlist_item_id"})
        if post.first_comment:
            calls.append({"method": "POST", "url": f"{API_BASE_URL}/commentThreads", "parameters": {"part": "snippet", "videoId": "{video_id}", "text": post.first_comment}, "headers": auth, "result": "comment_id"})
        calls.append({"method": "GET", "url": f"{API_BASE_URL}/videos", "parameters": {"part": "status,snippet", "id": "{video_id}"}, "headers": auth, "poll": {"until": "uploadStatus == processed"}})
        return calls

    # Publish -------------------------------------------------------------

    def publish(self, post: PostData, attempt: AttemptData, ctx: Ctx) -> ReceiptData | Pending:
        settings = self._settings(ctx)
        media = post.media[0]
        if ctx.dry_run:
            calls = self._plan(post, settings, media)
            ctx.transcript.extend(calls)
            return ReceiptData(external_id=f"dry-run:youtube:{post.external_ref}", raw={"dry_run": True, "api_calls": calls})

        path, temporary = self._local_file(media, ctx)
        try:
            size = path.stat().st_size
            if size <= 0:
                raise ConfigError("Video file is empty.", path=str(path))
            token = self._access_token(settings, ctx)
            self._verify_channel(token, settings, ctx)
            metadata = self._metadata(post, settings)
            ctx.checkpoint(
                {
                    "network_started": True,
                    "phase": "upload",
                    "title": metadata["snippet"]["title"],
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            start = self.transport.send(
                "POST",
                f"{UPLOAD_BASE_URL}/videos",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Upload-Content-Type": "video/*",
                    "X-Upload-Content-Length": str(size),
                },
                query={"uploadType": "resumable", "part": "snippet,status"},
                json_body=metadata,
                timeout=ctx.timeout_seconds,
            )
            session_url = start.headers.get("location", "")
            if not session_url:
                raise ApiError("YouTube did not open a resumable upload session.", definitive=True)
            ctx.checkpoint({"upload_session": session_url, "upload_size": size})
            video_id = self._stream(path, size, session_url, token, ctx)
            ctx.checkpoint({"video_id": video_id, "phase": "uploaded"})
        finally:
            if temporary:
                path.unlink(missing_ok=True)

        extras: dict[str, Any] = {}
        if settings["playlist_id"]:
            try:
                item = self._api(
                    "POST",
                    "playlistItems",
                    token,
                    ctx,
                    query={"part": "snippet"},
                    body={"snippet": {"playlistId": settings["playlist_id"], "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
                )
                extras["playlist_item_id"] = str(item.get("id") or "")
            except ApiError as exc:
                extras["playlist_error"] = exc.detail
            ctx.checkpoint(extras)
        if post.first_comment:
            try:
                comment = self._api(
                    "POST",
                    "commentThreads",
                    token,
                    ctx,
                    query={"part": "snippet"},
                    body={"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": post.first_comment}}}},
                )
                extras["first_comment_id"] = str(comment.get("id") or "")
            except ApiError as exc:
                extras["first_comment_error"] = exc.detail
            ctx.checkpoint(extras)
        return Pending({"video_id": video_id, **extras})

    def _stream(self, path: Path, size: int, session_url: str, token: str, ctx: Ctx) -> str:
        sent = 0
        payload: dict[str, Any] = {}
        with path.open("rb") as handle:
            while sent < size:
                chunk = handle.read(self.chunk_bytes)
                if not chunk:
                    raise ApiError("Video file shrank during upload.", definitive=True)
                end = sent + len(chunk) - 1
                response = self.transport.send(
                    "PUT",
                    session_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "video/*",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {sent}-{end}/{size}",
                    },
                    data=chunk,
                    timeout=max(ctx.timeout_seconds, 300),
                )
                sent = end + 1
                if response.status in {200, 201} and response.payload.get("id"):
                    payload = response.payload
                    break
        if not payload.get("id"):
            probe = self._probe_session(session_url, size, token, ctx)
            if probe.status in {200, 201}:
                payload = probe.payload
        video_id = str(payload.get("id") or "").strip()
        if not video_id:
            raise ApiError("YouTube finished the upload without returning a video id.", definitive=False)
        return video_id

    def _probe_session(self, session_url: str, size: int, token: str, ctx: Ctx) -> HttpResponse:
        return self.transport.send(
            "PUT",
            session_url,
            headers={"Authorization": f"Bearer {token}", "Content-Length": "0", "Content-Range": f"bytes */{size}"},
            data=b"",
            timeout=ctx.timeout_seconds,
        )

    # Poll ----------------------------------------------------------------

    def poll(self, pending: Pending, ctx: Ctx) -> ReceiptData | Pending | Failed:
        video_id = str(pending.handle.get("video_id") or "")
        if not video_id:
            return Failed("YouTube pending handle is invalid.")
        token = self._access_token(self._settings(ctx), ctx)
        payload = self._api("GET", "videos", token, ctx, query={"part": "status,snippet,processingDetails", "id": video_id})
        items = payload.get("items") or []
        if not items:
            return Failed(f"YouTube video {video_id} disappeared after upload.")
        item = items[0]
        status = item.get("status") or {}
        upload_status = str(status.get("uploadStatus") or "").lower()
        if upload_status in {"failed", "rejected", "deleted"}:
            reason = status.get("failureReason") or status.get("rejectionReason") or upload_status
            return Failed(f"YouTube video {video_id} {upload_status}: {reason}.")
        if upload_status != "processed":
            return Pending(dict(pending.handle))
        raw = {
            "video": item,
            "privacy": status.get("privacyStatus"),
            **{key: value for key, value in pending.handle.items() if key != "video_id"},
        }
        return ReceiptData(video_id, video_url(video_id), raw)

    # Lookup --------------------------------------------------------------

    def lookup(self, attempt: AttemptData, ctx: Ctx):
        settings = self._settings(ctx)
        remote = attempt.remote_ids
        video_id = str(remote.get("video_id") or "")
        session_url = str(remote.get("upload_session") or "")
        size = int(remote.get("upload_size") or 0)
        if not video_id and not session_url and not remote.get("network_started"):
            return NotFound()
        try:
            token = self._access_token(settings, ctx)
            if video_id:
                return self._lookup_video(video_id, token, ctx)
            if session_url and size:
                try:
                    probe = self._probe_session(session_url, size, token, ctx)
                except ApiError as exc:
                    if exc.not_found:
                        return NotFound()
                    raise
                if probe.status in {200, 201} and probe.payload.get("id"):
                    return self._lookup_video(str(probe.payload["id"]), token, ctx)
                return NotFound()  # 308: the session never completed, nothing was published.
            return self._scan_recent_uploads(remote, token, settings, ctx)
        except ApiError as exc:
            return Unknown(exc.detail)
        except ConfigError as exc:
            return Unknown(exc.detail)

    def _lookup_video(self, video_id: str, token: str, ctx: Ctx):
        payload = self._api("GET", "videos", token, ctx, query={"part": "status,snippet", "id": video_id})
        items = payload.get("items") or []
        if not items:
            return NotFound()
        item = items[0]
        return Found(ReceiptData(video_id, video_url(video_id), {"video": item, "privacy": (item.get("status") or {}).get("privacyStatus")}))

    def _scan_recent_uploads(self, remote: dict[str, Any], token: str, settings: dict[str, str], ctx: Ctx):
        """Match the attempt's title against the channel's newest uploads."""
        channel = self._verify_channel(token, settings, ctx)
        uploads = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if not uploads:
            return Unknown("YouTube did not expose the channel's uploads playlist.")
        payload = self._api("GET", "playlistItems", token, ctx, query={"part": "snippet", "playlistId": uploads, "maxResults": RECENT_UPLOADS_TO_SCAN})
        expected_title = str(remote.get("title") or "")
        started_at = str(remote.get("started_at") or "")
        for item in payload.get("items") or []:
            snippet = item.get("snippet") or {}
            video_id = str((snippet.get("resourceId") or {}).get("videoId") or "")
            published_at = str(snippet.get("publishedAt") or "")
            if video_id and snippet.get("title") == expected_title and (not started_at or published_at >= started_at[:19]):
                return self._lookup_video(video_id, token, ctx)
        return NotFound()

    # Edit, delete, insights --------------------------------------------

    def edit(self, receipt: ReceiptData, caption: str, ctx: Ctx) -> ReceiptData:
        settings = self._settings(ctx)
        token = self._access_token(settings, ctx)
        current = (receipt.raw.get("video") or {}).get("snippet") or {}
        if not current.get("title"):
            fetched = self._api("GET", "videos", token, ctx, query={"part": "snippet", "id": receipt.external_id})
            items = fetched.get("items") or []
            current = (items[0].get("snippet") if items else None) or {}
        snippet = {
            "title": current.get("title") or title_for(PostData(None, "", "youtube", "video", [], caption)),
            "description": caption.strip(),
            "categoryId": current.get("categoryId") or settings["category_id"],
        }
        if current.get("defaultLanguage"):
            snippet["defaultLanguage"] = current["defaultLanguage"]
        payload = self._api("PUT", "videos", token, ctx, query={"part": "snippet"}, body={"id": receipt.external_id, "snippet": snippet})
        return ReceiptData(receipt.external_id, receipt.permalink, {**receipt.raw, "edit": payload})

    def delete(self, receipt: ReceiptData, ctx: Ctx) -> None:
        settings = self._settings(ctx)
        token = self._access_token(settings, ctx)
        self._verify_channel(token, settings, ctx)
        self._api("DELETE", "videos", token, ctx, query={"id": receipt.external_id})

    def insights(self, receipts: list[ReceiptData], ctx: Ctx) -> list[MetricData]:
        if not receipts:
            return []
        settings = self._settings(ctx)
        token = self._access_token(settings, ctx)
        fetched_at = datetime.now(timezone.utc)
        result: list[MetricData] = []
        ids = [receipt.external_id for receipt in receipts]
        for offset in range(0, len(ids), 50):
            payload = self._api("GET", "videos", token, ctx, query={"part": "statistics", "id": ",".join(ids[offset : offset + 50])})
            for item in payload.get("items") or []:
                stats = item.get("statistics") or {}
                video_id = str(item.get("id") or "")
                for name, key in (("views", "viewCount"), ("likes", "likeCount"), ("comments", "commentCount")):
                    if key in stats:
                        result.append(MetricData(name, int(stats[key]), fetched_at, item, video_id))
        return result


# --- one-time OAuth consent ------------------------------------------------


def authorize_channel(
    *,
    client_id: str,
    client_secret: str,
    transport: JsonTransport | None = None,
    open_browser: bool = True,
    timeout_seconds: float = 300,
    announce=print,
) -> dict[str, Any]:
    """Loopback OAuth consent; returns the refresh token and the channel it unlocks."""
    import base64
    import hashlib
    import http.server
    import secrets
    import threading
    import webbrowser
    from urllib.parse import parse_qs, urlencode

    transport = transport or UrlLibJsonTransport()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            result.update({key: values[0] for key, values in parse_qs(urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<p>Postdesk: you can close this tab.</p>")

        def log_message(self, *_args) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    announce("Open this link, sign in as the channel owner, and pick the channel:")
    announce(url)
    if open_browser:
        webbrowser.open(url)
    thread.join(timeout_seconds)
    server.server_close()
    if not result:
        raise ConfigError("No login callback arrived before the timeout.", path="auth")
    if result.get("state") != state:
        raise ConfigError("Login callback state did not match.", path="auth")
    if "code" not in result:
        raise ConfigError(f"Google refused the login: {result.get('error', 'unknown error')}.", path="auth")
    tokens = transport.send(
        "POST",
        TOKEN_URL,
        headers={},
        form={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    ).payload
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise ConfigError("Google returned no refresh token; remove the app at myaccount.google.com/permissions and retry.", path="auth")
    granted = set(str(tokens.get("scope") or "").split())
    missing = [scope for scope in SCOPES if scope not in granted]
    if missing:
        raise ConfigError(f"The login did not grant required scopes: {', '.join(missing)}.", path="auth")
    channels = transport.send(
        "GET",
        f"{API_BASE_URL}/channels",
        headers={"Authorization": f"Bearer {tokens.get('access_token', '')}"},
        query={"part": "id,snippet", "mine": "true"},
        timeout=30,
    ).payload
    items = channels.get("items") or []
    if not items:
        raise ConfigError("That Google account has no YouTube channel.", path="auth")
    snippet = items[0].get("snippet") or {}
    return {
        "refresh_token": refresh_token,
        "channel_id": str(items[0].get("id") or ""),
        "channel_title": str(snippet.get("title") or ""),
        "handle": str(snippet.get("customUrl") or ""),
        "scopes": sorted(granted),
        "obtained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
