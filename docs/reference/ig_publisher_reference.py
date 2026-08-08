from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


GRAPH_API_BASE_URL = "https://graph.instagram.com/v23.0"
DEFAULT_INBOUND_MEDIA_ROOT = Path("/srv/app/shared/inbound-media")
DEFAULT_PUBLIC_MEDIA_ROOT = Path("/srv/app/shared/public-media")
DEFAULT_INSTANCE_PATH = Path("/srv/app/shared/instance")
SIGNED_URL_TTL_SECONDS = 3600
STATUS_MAX_POLLS = 20
STATUS_POLL_INTERVAL_SECONDS = 3

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".m4v", ".mov", ".mp4", ".webm"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


class InstagramPublishError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error: dict | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error or {}
        self.status_code = status_code


class InstagramMissingScopeError(InstagramPublishError):
    pass


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise InstagramPublishError(f"{name} is required.")
    return value


def configured_media_roots(
    *,
    inbound_root: str | Path | None = None,
    public_root: str | Path | None = None,
    instance_path: str | Path | None = None,
) -> dict[str, Path]:
    configured_inbound = (
        inbound_root
        or os.environ.get("REF_MEDIA_INBOUND_DIR")
        or DEFAULT_INBOUND_MEDIA_ROOT
    )
    configured_public = (
        public_root
        or os.environ.get("REF_PUBLIC_MEDIA_ROOT")
        or DEFAULT_PUBLIC_MEDIA_ROOT
    )
    configured_instance = (
        instance_path
        or os.environ.get("REF_INSTANCE_PATH")
        or DEFAULT_INSTANCE_PATH
    )
    return {
        "inbound": Path(configured_inbound).expanduser(),
        "public": Path(configured_public).expanduser(),
        "reels": Path(configured_instance).expanduser() / "reels",
    }


def media_kind(value: str) -> str:
    suffix = Path(urlparse(value).path).suffix.casefold()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    raise InstagramPublishError(
        f"Unsupported media type '{suffix or '(none)'}'. "
        f"Supported extensions: {', '.join(sorted(MEDIA_EXTENSIONS))}."
    )


def canonical_signed_path(root_name: str, relative_path: str | PurePosixPath) -> str:
    rel = PurePosixPath(str(relative_path))
    if (
        root_name not in {"inbound", "public", "reels"}
        or rel.is_absolute()
        or not rel.parts
        or ".." in rel.parts
    ):
        raise InstagramPublishError("Media path is outside the allowed media roots.")
    return f"{root_name}/{rel.as_posix()}"


def sign_media_path(path: str, expires: int, secret: str) -> str:
    if not secret:
        raise InstagramPublishError("REF_MEDIA_URL_SECRET is required for local media.")
    message = f"{path}\n{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_media_signature(path: str, expires: int, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = sign_media_path(path, expires, secret)
    return hmac.compare_digest(expected, signature)


def validate_public_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InstagramPublishError("REF_PUBLIC_BASE_URL must be an absolute HTTP(S) URL.")
    return value.rstrip("/")


def resolve_local_media(
    media_path: str | Path,
    *,
    roots: dict[str, Path],
) -> tuple[Path, str, PurePosixPath]:
    requested = Path(media_path).expanduser()
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InstagramPublishError(f"Media file is not readable: {requested}") from exc
    if not resolved.is_file():
        raise InstagramPublishError(f"Media file is not readable: {requested}")
    try:
        with resolved.open("rb"):
            pass
    except OSError as exc:
        raise InstagramPublishError(f"Media file is not readable: {requested}") from exc

    for root_name, root in roots.items():
        try:
            relative = resolved.relative_to(root.expanduser().resolve())
        except (OSError, ValueError):
            continue
        rel = PurePosixPath(relative.as_posix())
        canonical_signed_path(root_name, rel)
        return resolved, root_name, rel
    allowed = ", ".join(str(root) for root in roots.values())
    raise InstagramPublishError(
        f"Local media must be inside an allowed media root: {allowed}."
    )


def resolve_signed_media_file(
    root_name: str,
    relative_path: str,
    *,
    roots: dict[str, Path],
) -> tuple[Path, PurePosixPath] | None:
    root = roots.get(root_name)
    if root is None:
        return None
    try:
        canonical_signed_path(root_name, relative_path)
    except InstagramPublishError:
        return None
    rel = PurePosixPath(relative_path)
    try:
        resolved_root = root.expanduser().resolve()
        target = (resolved_root / rel.as_posix()).resolve()
        target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not target.is_file() or target.suffix.casefold() not in MEDIA_EXTENSIONS:
        return None
    return resolved_root, rel


def build_signed_media_url(
    media_path: str | Path,
    *,
    public_base_url: str,
    secret: str,
    roots: dict[str, Path] | None = None,
    now: float | None = None,
    ttl_seconds: int = SIGNED_URL_TTL_SECONDS,
) -> tuple[str, str]:
    if ttl_seconds < 1:
        raise InstagramPublishError("Signed media URL lifetime must be positive.")
    resolved, root_name, rel = resolve_local_media(
        media_path,
        roots=roots or configured_media_roots(),
    )
    kind = media_kind(resolved.name)
    expires = int(time.time() if now is None else now) + ttl_seconds
    signed_path = canonical_signed_path(root_name, rel)
    signature = sign_media_path(signed_path, expires, secret)
    encoded_rel = quote(rel.as_posix(), safe="/")
    query = urlencode({"expires": expires, "signature": signature})
    base_url = validate_public_base_url(public_base_url)
    url = f"{base_url}/public-media/signed/{root_name}/{encoded_rel}?{query}"
    return url, kind


def resolve_media_url(
    media: str | Path,
    *,
    public_base_url: str | None = None,
    media_url_secret: str | None = None,
    roots: dict[str, Path] | None = None,
    now: float | None = None,
) -> tuple[str, str]:
    value = str(media).strip()
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        if not parsed.netloc:
            raise InstagramPublishError("Media URL must include a host.")
        return value, media_kind(value)
    if parsed.scheme:
        raise InstagramPublishError("Media must be a local path or an HTTP(S) URL.")
    return build_signed_media_url(
        value,
        public_base_url=public_base_url or required_env("REF_PUBLIC_BASE_URL"),
        secret=media_url_secret or required_env("REF_MEDIA_URL_SECRET"),
        roots=roots,
        now=now,
    )


def missing_publish_scope(error: dict, message: str) -> bool:
    normalized = message.casefold()
    if "instagram_business_content_publish" in normalized:
        return True
    try:
        code = int(error.get("code"))
    except (TypeError, ValueError):
        code = None
    return code == 10 and "permission" in normalized


def instagram_api_error(
    payload: object,
    *,
    status_code: int | None = None,
    publishing_operation: bool = False,
) -> InstagramPublishError:
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or "").strip()
    if not message:
        suffix = f" HTTP {status_code}" if status_code is not None else ""
        message = f"Instagram API request failed{suffix}."
    if publishing_operation and missing_publish_scope(error, message):
        return InstagramMissingScopeError(
            f"{message} Re-consent the Instagram account with "
            "instagram_business_content_publish."
        )
    return InstagramPublishError(message, error=error, status_code=status_code)


class InstagramApiClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = GRAPH_API_BASE_URL,
        timeout_seconds: float = 30,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        parameters: dict[str, object],
        publishing_operation: bool = False,
    ) -> dict:
        values = {**parameters, "access_token": self.token}
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None
        if method == "GET":
            url = f"{url}?{urlencode(values)}"
        else:
            data = urlencode(values).encode("utf-8")
        request = Request(
            url,
            data=data,
            headers={"Accept": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            raise instagram_api_error(
                payload,
                status_code=exc.code,
                publishing_operation=publishing_operation,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise InstagramPublishError(f"Instagram API is unavailable: {exc}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstagramPublishError("Instagram API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise InstagramPublishError("Instagram API returned invalid data.")
        if "error" in payload:
            raise instagram_api_error(
                payload,
                publishing_operation=publishing_operation,
            )
        return payload


def _container_parameters(
    media_url: str,
    kind: str,
    *,
    caption: str | None = None,
    story: bool = False,
    carousel_item: bool = False,
    collaborators: Sequence[str] = (),
    location_id: str | None = None,
    alt_text: str | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {}
    if kind == "video":
        parameters["video_url"] = media_url
        if story:
            parameters["media_type"] = "STORIES"
        elif not carousel_item:
            parameters["media_type"] = "REELS"
    else:
        parameters["image_url"] = media_url
        if story:
            parameters["media_type"] = "STORIES"
        if alt_text:
            parameters["alt_text"] = alt_text
    if caption is not None and not story:
        parameters["caption"] = caption
    if carousel_item:
        parameters["is_carousel_item"] = "true"
    if collaborators:
        parameters["collaborators"] = ",".join(collaborators)
    if location_id:
        parameters["location_id"] = location_id
    return parameters


def _planned_call(method: str, url: str, parameters: dict, **extra) -> dict:
    return {
        "method": method,
        "url": url,
        "parameters": {**parameters, "access_token": "<REF_IG_PUBLISH_TOKEN>"},
        **extra,
    }


def _status_plan(identifier: str) -> dict:
    return _planned_call(
        "GET",
        f"{GRAPH_API_BASE_URL}/{identifier}",
        {"fields": "status_code"},
        poll={
            "until": "FINISHED",
            "max_attempts": STATUS_MAX_POLLS,
            "interval_seconds": STATUS_POLL_INTERVAL_SECONDS,
        },
    )


def api_call_plan(
    *,
    ig_user_id: str,
    media_urls: Sequence[str],
    kinds: Sequence[str],
    caption: str,
    story: bool = False,
    first_comment: str | None = None,
    collaborators: Sequence[str] = (),
    location_id: str | None = None,
    alt_text: str | None = None,
) -> list[dict]:
    calls: list[dict] = []
    if len(media_urls) > 1:
        for index, (media_url, kind) in enumerate(zip(media_urls, kinds, strict=True)):
            calls.append(
                _planned_call(
                    "POST",
                    f"{GRAPH_API_BASE_URL}/{ig_user_id}/media",
                    _container_parameters(
                        media_url,
                        kind,
                        carousel_item=True,
                        alt_text=alt_text,
                    ),
                    result=f"child_creation_id_{index + 1}",
                )
            )
            if kind == "video":
                calls.append(_status_plan(f"{{child_creation_id_{index + 1}}}"))
        parent_parameters: dict[str, object] = {
            "media_type": "CAROUSEL",
            "children": ",".join(
                f"{{child_creation_id_{index + 1}}}"
                for index in range(len(media_urls))
            ),
            "caption": caption,
        }
        if collaborators:
            parent_parameters["collaborators"] = ",".join(collaborators)
        if location_id:
            parent_parameters["location_id"] = location_id
        calls.append(
            _planned_call(
                "POST",
                f"{GRAPH_API_BASE_URL}/{ig_user_id}/media",
                parent_parameters,
                result="creation_id",
            )
        )
    else:
        calls.append(
            _planned_call(
                "POST",
                f"{GRAPH_API_BASE_URL}/{ig_user_id}/media",
                _container_parameters(
                    media_urls[0],
                    kinds[0],
                    caption=caption,
                    story=story,
                    collaborators=() if story else collaborators,
                    location_id=location_id,
                    alt_text=alt_text,
                ),
                result="creation_id",
            )
        )
    calls.append(_status_plan("{creation_id}"))
    calls.append(
        _planned_call(
            "POST",
            f"{GRAPH_API_BASE_URL}/{ig_user_id}/media_publish",
            {"creation_id": "{creation_id}"},
            result="media_id",
        )
    )
    calls.append(
        _planned_call(
            "GET",
            f"{GRAPH_API_BASE_URL}/{{media_id}}",
            {"fields": "permalink"},
        )
    )
    if first_comment and first_comment.strip():
        calls.append(
            _planned_call(
                "POST",
                f"{GRAPH_API_BASE_URL}/{{media_id}}/comments",
                {"message": first_comment.strip()},
            )
        )
    return calls


def response_id(payload: dict, label: str) -> str:
    value = str(payload.get("id") or "").strip()
    if not value:
        raise InstagramPublishError(f"Instagram API did not return a {label} id.")
    return value


def _normalize_media(media: str | Path | Sequence[str | Path]) -> list[str | Path]:
    if isinstance(media, (str, Path)):
        return [media]
    if not isinstance(media, Sequence):
        raise InstagramPublishError("Media must be one value or a sequence.")
    values = list(media)
    if not values:
        raise InstagramPublishError("At least one media value is required.")
    if len(values) > 10:
        raise InstagramPublishError("Instagram accepts at most 10 carousel items.")
    if not all(isinstance(value, (str, Path)) for value in values):
        raise InstagramPublishError("Every media value must be a path or URL.")
    return values


def _clean_collaborators(values: Sequence[str]) -> tuple[str, ...]:
    cleaned = tuple(value.strip().lstrip("@").strip() for value in values)
    if any(not value for value in cleaned):
        raise InstagramPublishError("Collaborator usernames must not be blank.")
    return cleaned


def _parameter_rejection(exc: InstagramPublishError) -> bool:
    message = str(exc).casefold()
    try:
        code = int(exc.error.get("code"))
    except (TypeError, ValueError):
        code = None
    return code in {100, 36003} or any(
        phrase in message
        for phrase in ("invalid parameter", "unsupported parameter", "unknown field")
    )


def _rejected_optional(exc: InstagramPublishError) -> str | None:
    if not _parameter_rejection(exc):
        return None
    message = str(exc).casefold()
    has_collaborators = "collaborator" in message
    has_alt_text = "alt_text" in message or "alt text" in message
    if has_collaborators and not has_alt_text:
        return "collaborators"
    if has_alt_text and not has_collaborators:
        return "alt_text"
    return "both"


def _create_container_with_fallback(
    api: InstagramApiClient,
    path: str,
    parameters: dict[str, object],
) -> tuple[str, bool, bool]:
    collaborators_unsupported = False
    alt_text_unsupported = False
    current = dict(parameters)
    try:
        payload = api.request(
            "POST", path, parameters=current, publishing_operation=True
        )
    except InstagramPublishError as exc:
        rejected = _rejected_optional(exc)
        removable = {
            "collaborators": "collaborators" in current,
            "alt_text": "alt_text" in current,
        }
        if rejected is None or not any(removable.values()):
            raise
        if rejected == "collaborators" and removable["collaborators"]:
            current.pop("collaborators", None)
            collaborators_unsupported = True
        elif rejected == "alt_text" and removable["alt_text"]:
            current.pop("alt_text", None)
            alt_text_unsupported = True
        else:
            collaborators_unsupported = removable["collaborators"]
            alt_text_unsupported = removable["alt_text"]
            current.pop("collaborators", None)
            current.pop("alt_text", None)
        logger.warning("Instagram rejected an optional publishing parameter; retrying without it.")
        payload = api.request(
            "POST", path, parameters=current, publishing_operation=True
        )
    return (
        response_id(payload, "creation"),
        collaborators_unsupported,
        alt_text_unsupported,
    )


def _wait_until_ready(
    api: InstagramApiClient,
    creation_id: str,
    *,
    sleep_for: Callable[[float], None],
    max_polls: int,
    poll_interval_seconds: float,
) -> None:
    for attempt in range(1, max_polls + 1):
        status_payload = api.request(
            "GET", creation_id, parameters={"fields": "status_code"}
        )
        status = str(status_payload.get("status_code") or "").strip().upper()
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise InstagramPublishError(
                f"Instagram media container {creation_id} reached ERROR status."
            )
        if attempt == max_polls:
            raise InstagramPublishError(
                f"Instagram media container {creation_id} did not finish after "
                f"{max_polls} status checks."
            )
        sleep_for(poll_interval_seconds)


def publish_media(
    media: str | Path | Sequence[str | Path],
    caption: str,
    *,
    story: bool = False,
    first_comment: str | None = None,
    collaborators: Sequence[str] = (),
    location_id: str | None = None,
    alt_text: str | None = None,
    dry_run: bool = False,
    roots: dict[str, Path] | None = None,
    client: InstagramApiClient | None = None,
    sleep: Callable[[float], None] | None = None,
    max_polls: int = STATUS_MAX_POLLS,
    poll_interval_seconds: float = STATUS_POLL_INTERVAL_SECONDS,
    now: float | None = None,
    organizer: str = "demotenant",
) -> dict:
    clean_caption = caption.strip()
    if not clean_caption:
        raise InstagramPublishError("Caption is required.")
    if max_polls < 1:
        raise InstagramPublishError("Container poll count must be positive.")

    media_values = _normalize_media(media)
    if story and len(media_values) != 1:
        raise InstagramPublishError("A story requires exactly one media value.")
    clean_collaborators = _clean_collaborators(collaborators)
    clean_first_comment = (first_comment or "").strip() or None
    clean_location_id = (location_id or "").strip() or None
    clean_alt_text = (alt_text or "").strip() or None

    from .reel_brand import BrandKitError, resolve_publish_credentials

    try:
        credentials = resolve_publish_credentials(organizer)
    except BrandKitError as exc:
        raise InstagramPublishError(str(exc)) from exc
    token = credentials["token"]
    ig_user_id = credentials["ig_user_id"]
    sleep_for = sleep or time.sleep
    resolved = [
        resolve_media_url(item, roots=roots, now=now) for item in media_values
    ]
    resolved_urls = [item[0] for item in resolved]
    kinds = [item[1] for item in resolved]
    calls = api_call_plan(
        ig_user_id=ig_user_id,
        media_urls=resolved_urls,
        kinds=kinds,
        caption=clean_caption,
        story=story,
        first_comment=clean_first_comment,
        collaborators=clean_collaborators,
        location_id=clean_location_id,
        alt_text=clean_alt_text,
    )
    result_kind = "story" if story else ("carousel" if len(resolved) > 1 else kinds[0])
    common_result = {
        "media_kind": result_kind,
        "media_urls": resolved_urls,
        "first_comment_posted": False,
        "first_comment_error": None,
        "collaborators_unsupported": False,
        "alt_text_unsupported": False,
        "organizer_id": organizer,
    }
    if len(resolved_urls) == 1:
        common_result["media_url"] = resolved_urls[0]
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            **common_result,
            "caption": clean_caption,
            "creation_id": None,
            "child_creation_ids": [],
            "api_calls": calls,
        }

    api = client or InstagramApiClient(token=token)
    child_creation_ids: list[str] = []
    collaborators_unsupported = False
    alt_text_unsupported = False

    if len(resolved_urls) > 1:
        effective_alt_text = clean_alt_text
        for media_url, kind in zip(resolved_urls, kinds, strict=True):
            child_id, _child_collab_fallback, child_alt_fallback = (
                _create_container_with_fallback(
                    api,
                    f"{ig_user_id}/media",
                    _container_parameters(
                        media_url,
                        kind,
                        carousel_item=True,
                        alt_text=effective_alt_text,
                    ),
                )
            )
            alt_text_unsupported = alt_text_unsupported or child_alt_fallback
            if child_alt_fallback:
                effective_alt_text = None
            child_creation_ids.append(child_id)
            if kind == "video":
                _wait_until_ready(
                    api,
                    child_id,
                    sleep_for=sleep_for,
                    max_polls=max_polls,
                    poll_interval_seconds=poll_interval_seconds,
                )
        parent_parameters: dict[str, object] = {
            "media_type": "CAROUSEL",
            "children": ",".join(child_creation_ids),
            "caption": clean_caption,
        }
        if clean_collaborators:
            parent_parameters["collaborators"] = ",".join(clean_collaborators)
        if clean_location_id:
            parent_parameters["location_id"] = clean_location_id
        creation_id, collaborators_unsupported, _parent_alt_fallback = (
            _create_container_with_fallback(
                api, f"{ig_user_id}/media", parent_parameters
            )
        )
    else:
        creation_id, collaborators_unsupported, alt_text_unsupported = (
            _create_container_with_fallback(
                api,
                f"{ig_user_id}/media",
                _container_parameters(
                    resolved_urls[0],
                    kinds[0],
                    caption=clean_caption,
                    story=story,
                    collaborators=() if story else clean_collaborators,
                    location_id=clean_location_id,
                    alt_text=clean_alt_text,
                ),
            )
        )

    _wait_until_ready(
        api,
        creation_id,
        sleep_for=sleep_for,
        max_polls=max_polls,
        poll_interval_seconds=poll_interval_seconds,
    )

    published = api.request(
        "POST",
        f"{ig_user_id}/media_publish",
        parameters={"creation_id": creation_id},
        publishing_operation=True,
    )
    media_id = response_id(published, "published media")
    media_details = api.request(
        "GET",
        media_id,
        parameters={"fields": "permalink"},
    )
    permalink = str(media_details.get("permalink") or "").strip() or None
    if not permalink and not story:
        raise InstagramPublishError("Instagram API did not return a permalink.")
    first_comment_posted = False
    first_comment_error = None
    if clean_first_comment:
        try:
            api.request(
                "POST",
                f"{media_id}/comments",
                parameters={"message": clean_first_comment},
                publishing_operation=True,
            )
            first_comment_posted = True
        except InstagramPublishError as exc:
            first_comment_error = str(exc)
            logger.warning("Instagram first comment failed after publication.")

    return {
        "ok": True,
        "dry_run": False,
        "creation_id": creation_id,
        "child_creation_ids": child_creation_ids,
        "media_id": media_id,
        "permalink": permalink,
        **common_result,
        "caption": clean_caption,
        "first_comment_posted": first_comment_posted,
        "first_comment_error": first_comment_error,
        "collaborators_unsupported": collaborators_unsupported,
        "alt_text_unsupported": alt_text_unsupported,
    }
