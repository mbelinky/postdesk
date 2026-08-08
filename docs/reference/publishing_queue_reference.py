from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .ig_publisher import (
    InstagramPublishError,
    configured_media_roots,
    media_kind,
    publish_media,
    resolve_local_media,
)
from .models import PublishingQueuePost


logger = logging.getLogger(__name__)
MADRID = ZoneInfo("Europe/Madrid")
QUEUE_STATUSES = {"draft", "approved", "published", "failed", "cancelled"}
MAX_ERROR_LENGTH = 500


class PublishingQueueError(RuntimeError):
    pass


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_madrid_publish_at(value: str) -> datetime:
    try:
        wall = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise PublishingQueueError(
            "Publish time must use minute precision: YYYY-MM-DDTHH:MM."
        ) from exc
    candidates: set[datetime] = set()
    for fold in (0, 1):
        aware = wall.replace(tzinfo=MADRID, fold=fold)
        utc = aware.astimezone(timezone.utc)
        round_trip = utc.astimezone(MADRID)
        if round_trip.replace(tzinfo=None) == wall and round_trip.fold == fold:
            candidates.add(utc.replace(tzinfo=None))
    if len(candidates) != 1:
        raise PublishingQueueError(
            "Publish time is ambiguous or nonexistent in Europe/Madrid."
        )
    return candidates.pop()


def madrid_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc).astimezone(MADRID)


def canonicalize_media(
    value: str | Path,
    *,
    roots: dict[str, Path] | None = None,
) -> dict[str, str]:
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        raise PublishingQueueError("Queue media must be a local file.")
    if parsed.scheme:
        raise PublishingQueueError("Queue media must be a local file.")
    try:
        resolved, root_name, relative = resolve_local_media(
            raw,
            roots=roots or configured_media_roots(),
        )
        media_kind(resolved.name)
    except InstagramPublishError as exc:
        raise PublishingQueueError(str(exc)) from exc
    return {"root": root_name, "path": relative.as_posix()}


def resolve_canonical_media(
    reference: dict,
    *,
    roots: dict[str, Path] | None = None,
) -> Path:
    if not isinstance(reference, dict) or set(reference) != {"root", "path"}:
        raise PublishingQueueError("Stored media reference is invalid.")
    root_name = str(reference["root"])
    rel = PurePosixPath(str(reference["path"]))
    configured = roots or configured_media_roots()
    root = configured.get(root_name)
    if root is None or rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise PublishingQueueError("Stored media reference is outside allowed roots.")
    try:
        resolved_root = root.expanduser().resolve()
        target = (resolved_root / rel.as_posix()).resolve(strict=True)
        target.relative_to(resolved_root)
        if not target.is_file():
            raise OSError
        with target.open("rb"):
            pass
        media_kind(target.name)
    except (OSError, RuntimeError, ValueError, InstagramPublishError) as exc:
        raise PublishingQueueError(
            f"Queued media file is unavailable: {root_name}/{rel.as_posix()}."
        ) from exc
    return target


def add_post(
    session: Session,
    *,
    publish_at: str,
    media: Sequence[str | Path],
    caption: str,
    story_media: str | Path | None = None,
    first_comment: str | None = None,
    collaborators: Sequence[str] = (),
    location_id: str | None = None,
    alt_text: str | None = None,
    roots: dict[str, Path] | None = None,
) -> PublishingQueuePost:
    values = list(media)
    if not values:
        raise PublishingQueueError("At least one feed media file is required.")
    if len(values) > 10:
        raise PublishingQueueError("Instagram accepts at most 10 carousel items.")
    clean_caption = caption.strip()
    if not clean_caption:
        raise PublishingQueueError("Caption is required.")
    media_refs = [canonicalize_media(value, roots=roots) for value in values]
    story_ref = canonicalize_media(story_media, roots=roots) if story_media else None
    clean_collaborators = [value.strip().lstrip("@").strip() for value in collaborators]
    if any(not value for value in clean_collaborators):
        raise PublishingQueueError("Collaborator usernames must not be blank.")
    row = PublishingQueuePost(
        publish_at_utc=parse_madrid_publish_at(publish_at),
        media_json=json.dumps(media_refs, separators=(",", ":")),
        story_media_json=(
            json.dumps(story_ref, separators=(",", ":")) if story_ref else None
        ),
        caption=clean_caption,
        first_comment=(first_comment or "").strip() or None,
        collaborators_json=json.dumps(clean_collaborators, separators=(",", ":")),
        location_id=(location_id or "").strip() or None,
        alt_text=(alt_text or "").strip() or None,
        status="draft",
    )
    session.add(row)
    session.flush()
    return row


def serialize_post(row: PublishingQueuePost) -> dict:
    local = madrid_datetime(row.publish_at_utc)
    return {
        "id": row.id,
        "publish_at_utc": row.publish_at_utc.isoformat(timespec="minutes"),
        "publish_at": local.isoformat(timespec="minutes"),
        "media": json.loads(row.media_json),
        "story_media": json.loads(row.story_media_json) if row.story_media_json else None,
        "caption": row.caption,
        "first_comment": row.first_comment,
        "collaborators": json.loads(row.collaborators_json),
        "location_id": row.location_id,
        "alt_text": row.alt_text,
        "status": row.status,
        "claimed": row.claim_token is not None,
        "claimed_at_utc": row.claimed_at_utc.isoformat() if row.claimed_at_utc else None,
        "publish_attempted_at_utc": (
            row.publish_attempted_at_utc.isoformat()
            if row.publish_attempted_at_utc
            else None
        ),
        "published_at_utc": (
            row.published_at_utc.isoformat() if row.published_at_utc else None
        ),
        "media_id": row.media_id,
        "permalink": row.permalink,
        "result": json.loads(row.result_json) if row.result_json else None,
        "error": row.error_text,
    }


def list_posts(
    session: Session, *, status: str | None = None
) -> list[PublishingQueuePost]:
    if status is not None and status not in QUEUE_STATUSES:
        raise PublishingQueueError(f"Unknown queue status: {status}.")
    query = select(PublishingQueuePost)
    if status:
        query = query.where(PublishingQueuePost.status == status)
    return list(
        session.scalars(
            query.order_by(PublishingQueuePost.publish_at_utc, PublishingQueuePost.id)
        )
    )


def _get_post(session: Session, post_id: int) -> PublishingQueuePost:
    row = session.get(PublishingQueuePost, post_id)
    if row is None:
        raise PublishingQueueError(f"Queue post {post_id} was not found.")
    return row


def approve_post(session: Session, post_id: int) -> PublishingQueuePost:
    row = _get_post(session, post_id)
    if row.status != "draft" or row.claim_token is not None:
        raise PublishingQueueError("Only an unclaimed draft can be approved.")
    row.status = "approved"
    session.flush()
    return row


def cancel_post(session: Session, post_id: int) -> PublishingQueuePost:
    row = _get_post(session, post_id)
    if row.status not in {"draft", "approved"} or row.claim_token is not None:
        raise PublishingQueueError(
            "Only an unclaimed draft or approved post can be cancelled."
        )
    row.status = "cancelled"
    session.flush()
    return row


def parse_notify_template(value: str) -> list[str] | None:
    if not value.strip():
        return None
    try:
        argv = shlex.split(value)
    except ValueError as exc:
        raise PublishingQueueError("REF_PUBLISH_NOTIFY_CMD is not valid shell-style argv.") from exc
    if argv.count("{message}") != 1:
        raise PublishingQueueError(
            "REF_PUBLISH_NOTIFY_CMD must contain exactly one standalone {message} argument."
        )
    return argv


def notify(message: str, *, template: str | None = None) -> bool:
    argv = parse_notify_template(
        os.environ.get("REF_PUBLISH_NOTIFY_CMD", "") if template is None else template
    )
    if argv is None:
        return False
    command = [message if value == "{message}" else value for value in argv]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            check=False,
            timeout=30,
            text=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Publishing queue notification failed.")
        return False
    if completed.returncode != 0:
        logger.warning("Publishing queue notification returned a nonzero status.")
        return False
    return True


def _emit_notification(notifier: Callable[[str], object], message: str) -> None:
    try:
        notifier(message)
    except Exception:
        logger.warning("Publishing queue notification failed.")


def _bounded_error(exc: BaseException) -> str:
    value = " ".join(str(exc).split()) or exc.__class__.__name__
    secret_names = {
        "REF_META_ADS_TOKEN",
        "REF_MEDIA_URL_SECRET",
        "REF_IG_PUBLISH_TOKEN",
        *(
            name
            for name in os.environ
            if name.startswith("REF_IG_") and name.endswith("_PUBLISH_TOKEN")
        ),
    }
    for name in secret_names:
        secret = os.environ.get(name, "")
        if secret:
            value = value.replace(secret, "[redacted]")
    return value[:MAX_ERROR_LENGTH]


def _claim_post(engine, post_id: int, now: datetime) -> str | None:
    token = secrets.token_hex(16)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        result = connection.execute(
            text(
                "UPDATE publishing_queue_posts "
                "SET claim_token = :token, claimed_at_utc = :now, "
                "publish_attempted_at_utc = :now, updated_at = :now "
                "WHERE id = :id AND status = 'approved' "
                "AND claim_token IS NULL AND publish_at_utc <= :now"
            ),
            {"token": token, "now": now, "id": post_id},
        )
        connection.commit()
        return token if result.rowcount == 1 else None


def run_due_posts(
    session: Session,
    *,
    limit: int = 20,
    now: datetime | None = None,
    publisher: Callable[..., dict] = publish_media,
    roots: dict[str, Path] | None = None,
    notifier: Callable[[str], object] = notify,
    organizer: str = "demotenant",
) -> dict:
    if limit < 1:
        raise PublishingQueueError("Queue run limit must be positive.")
    if notifier is notify:
        parse_notify_template(os.environ.get("REF_PUBLISH_NOTIFY_CMD", ""))
    claim_now = now or utcnow_naive()
    engine = session.get_bind()
    candidate_ids = list(
        session.scalars(
            select(PublishingQueuePost.id)
            .where(
                PublishingQueuePost.status == "approved",
                PublishingQueuePost.claim_token.is_(None),
                PublishingQueuePost.publish_at_utc <= claim_now,
            )
            .order_by(PublishingQueuePost.publish_at_utc, PublishingQueuePost.id)
            .limit(limit)
        )
    )
    session.rollback()
    results: list[dict] = []
    for post_id in candidate_ids:
        token = _claim_post(engine, post_id, claim_now)
        if token is None:
            results.append({"id": post_id, "outcome": "skipped"})
            continue
        with Session(engine) as work:
            row = _get_post(work, post_id)
            try:
                media_refs = json.loads(row.media_json)
                feed_paths = [
                    resolve_canonical_media(item, roots=roots) for item in media_refs
                ]
                feed_result = publisher(
                    feed_paths,
                    row.caption,
                    first_comment=row.first_comment,
                    collaborators=json.loads(row.collaborators_json),
                    location_id=row.location_id,
                    alt_text=row.alt_text,
                    organizer=organizer,
                )
                combined: dict[str, object] = {"feed": feed_result, "story": None}
                if row.story_media_json:
                    story_path = resolve_canonical_media(
                        json.loads(row.story_media_json), roots=roots
                    )
                    try:
                        combined["story"] = publisher(
                            story_path,
                            row.caption,
                            story=True,
                            organizer=organizer,
                        )
                    except Exception:
                        row.result_json = json.dumps(combined, ensure_ascii=False)
                        raise
                row.status = "published"
                row.media_id = str(feed_result.get("media_id") or "") or None
                row.permalink = str(feed_result.get("permalink") or "") or None
                row.result_json = json.dumps(combined, ensure_ascii=False)
                row.error_text = None
                row.published_at_utc = utcnow_naive()
                row.claim_token = None
                work.commit()
                _emit_notification(
                    notifier,
                    f"Publishing queue post {post_id} published successfully.",
                )
                results.append({"id": post_id, "outcome": "published"})
            except Exception as exc:
                row.status = "failed"
                row.error_text = _bounded_error(exc)
                row.claim_token = None
                work.commit()
                _emit_notification(notifier, f"Publishing queue post {post_id} failed.")
                results.append({"id": post_id, "outcome": "failed", "error": row.error_text})
    return {
        "ok": True,
        "due": len(candidate_ids),
        "published": sum(item["outcome"] == "published" for item in results),
        "failed": sum(item["outcome"] == "failed" for item in results),
        "skipped": sum(item["outcome"] == "skipped" for item in results),
        "results": results,
    }


def reconcile_post(
    session: Session,
    post_id: int,
    *,
    published: bool,
    media_id: str | None = None,
    permalink: str | None = None,
    error: str | None = None,
) -> PublishingQueuePost:
    row = _get_post(session, post_id)
    if row.claim_token is None or row.status != "approved":
        raise PublishingQueueError(
            "Only an approved post with a durable unknown claim can be reconciled."
        )
    if published:
        clean_media_id = (media_id or "").strip()
        if not clean_media_id or error:
            raise PublishingQueueError("Published reconciliation requires a media id only.")
        row.status = "published"
        row.media_id = clean_media_id
        row.permalink = (permalink or "").strip() or None
        row.published_at_utc = utcnow_naive()
        row.error_text = None
    else:
        clean_error = (error or "").strip()
        if not clean_error or media_id or permalink:
            raise PublishingQueueError("Failed reconciliation requires an error only.")
        row.status = "failed"
        row.error_text = _bounded_error(PublishingQueueError(clean_error))
    row.claim_token = None
    session.flush()
    return row


def preview_posts(
    session: Session,
    *,
    days: int = 8,
    now: datetime | None = None,
) -> str:
    if days < 0:
        raise PublishingQueueError("Preview days cannot be negative.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_utc = current.astimezone(timezone.utc)
    current_local = current_utc.astimezone(MADRID)
    last_date = current_local.date() + timedelta(days=days)
    rows = list_posts(session)
    selected = [
        row
        for row in rows
        if row.status in {"draft", "approved"}
        and (local := madrid_datetime(row.publish_at_utc)) >= current_local
        and local.date() <= last_date
    ]
    blocks: list[str] = []
    for row in selected:
        local = madrid_datetime(row.publish_at_utc)
        media_names = ", ".join(
            PurePosixPath(item["path"]).name for item in json.loads(row.media_json)
        )
        lines = [
            f"Queue #{row.id} | {local:%Y-%m-%d %H:%M} Europe/Madrid | {row.status}",
            f"Feed: {media_names}",
        ]
        if row.story_media_json:
            story = json.loads(row.story_media_json)
            lines.append(f"Story: {PurePosixPath(story['path']).name}")
        lines.append("Caption:")
        lines.append(row.caption)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
