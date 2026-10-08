from __future__ import annotations

import json
import secrets
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Select, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import TenantConfig, load_tenant
from .errors import ApiError, ClaimConflict, ConfigError, InvalidInput, PostdeskError
from .hooks import hook_timeout, notify, resolve_media
from .models import Metric, Post, PublishAttempt, Receipt
from .types import AttemptData, Ctx, Failed, Found, NotFound, Pending, PostData, ReceiptData, Unknown

LEASE_SECONDS = 300
POLL_LIMITS = {
    ("instagram", "photo"): (20, 3.0),
    ("instagram", "carousel"): (20, 3.0),
    ("instagram", "reel"): (40, 3.0),
    ("instagram", "story"): (40, 3.0),
    ("facebook", "video"): (40, 5.0),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime:
    if not value:
        return utcnow()
    clean = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError as exc:
        raise InvalidInput("Time must be ISO 8601.", path="at") from exc
    if parsed.tzinfo is None:
        raise InvalidInput("Time must include a UTC offset or Z.", path="at")
    return parsed.astimezone(timezone.utc)


def add_post(
    session: Session,
    store: Path,
    drivers: dict[str, Any],
    *,
    tenant: str,
    channel: str,
    kind: str,
    media: list[str],
    caption: str | None,
    first_comment: str | None,
    collaborators: list[str],
    link: str | None,
    at: str | None,
    external_ref: str | None,
    approved: bool = False,
) -> tuple[Post, bool]:
    load_tenant(store, tenant)
    driver = drivers.get(channel)
    if driver is None:
        raise InvalidInput(f"Channel {channel!r} is not supported.", path="channel")
    clean_ref = (external_ref or "").strip() or str(uuid.uuid4())
    existing = session.scalar(
        select(Post).where(Post.tenant == tenant, Post.external_ref == clean_ref)
    )
    if existing is not None:
        return existing, False
    data = PostData(
        id=None,
        tenant=tenant,
        channel=channel,
        kind=kind,
        media=list(media),
        caption=(caption or "").strip() or None,
        first_comment=(first_comment or "").strip() or None,
        collaborators=[item.strip().lstrip("@") for item in collaborators],
        link=(link or "").strip() or None,
        external_ref=clean_ref,
    )
    problems = driver.validate(data)
    if problems:
        raise InvalidInput(" ".join(problems), path=f"capabilities.{channel}.{kind}")
    now = utcnow()
    row = Post(
        tenant=tenant,
        external_ref=clean_ref,
        channel=channel,
        kind=kind,
        media_json=json.dumps(data.media),
        caption=data.caption,
        first_comment=data.first_comment,
        collaborators_json=json.dumps(data.collaborators),
        link=data.link,
        scheduled_at=parse_time(at),
        status="approved" if approved else "draft",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Post).where(Post.tenant == tenant, Post.external_ref == clean_ref)
        )
        if existing is None:
            raise
        return existing, False
    return row, True


def serialize_post(row: Post, *, session: Session | None = None) -> dict[str, Any]:
    result = {
        "id": row.id,
        "tenant": row.tenant,
        "external_ref": row.external_ref,
        "channel": row.channel,
        "kind": row.kind,
        "media": json.loads(row.media_json),
        "caption": row.caption,
        "first_comment": row.first_comment,
        "collaborators": json.loads(row.collaborators_json),
        "link": row.link,
        "at": _iso(row.scheduled_at),
        "status": row.status,
        "claimed": bool(row.claim_token),
        "claim_expires_at": _iso(row.claim_expires_at),
        "error": row.error,
        "notify_error": row.notify_error,
    }
    if session is not None:
        receipts = list(session.scalars(select(Receipt).where(Receipt.post_id == row.id).order_by(Receipt.id)))
        result["receipts"] = [serialize_receipt(item) for item in receipts]
        attempts = list(session.scalars(select(PublishAttempt).where(PublishAttempt.post_id == row.id).order_by(PublishAttempt.id)))
        result["attempts"] = [serialize_attempt(item) for item in attempts]
    return result


def serialize_attempt(row: PublishAttempt) -> dict[str, Any]:
    return {
        "id": row.id,
        "state": row.state,
        "remote_ids": json.loads(row.remote_ids_json),
        "pending": json.loads(row.pending_json) if row.pending_json else None,
        "error": row.error,
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
    }


def serialize_receipt(row: Receipt) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "post_id": row.post_id,
        "channel": row.channel,
        "external_id": row.external_id,
        "permalink": row.permalink,
        "raw": json.loads(row.raw_json),
        "published_at": _iso(row.published_at),
        "deleted_at": _iso(row.deleted_at),
    }


def list_posts(session: Session, *, status: str | None = None, tenant: str | None = None) -> list[Post]:
    query: Select = select(Post)
    if status:
        query = query.where(Post.status == status)
    if tenant:
        query = query.where(Post.tenant == tenant)
    return list(session.scalars(query.order_by(Post.scheduled_at, Post.id)))


def get_post(session: Session, post_id: int) -> Post:
    row = session.get(Post, post_id)
    if row is None:
        raise InvalidInput(f"Post {post_id} was not found.", path="post_id")
    return row


def approve_post(session: Session, post_id: int, *, at: str | None = None, now: bool = False) -> Post:
    # The conditional write also protects against a worker claiming the row
    # between the operator's read and approval.
    values = {"status": "approved", "updated_at": utcnow()}
    if at is not None or now:
        values["scheduled_at"] = parse_time(at if not now else None)
    changed = session.execute(
        update(Post).where(Post.id == post_id, Post.status.in_(["draft", "approved"]), Post.claim_token.is_(None)).values(**values)
    )
    session.commit()
    session.expire_all()
    row = get_post(session, post_id)
    if changed.rowcount != 1 and row.status != "published":
        raise ClaimConflict("Only an unclaimed draft or approved post can be scheduled.", path="post_id")
    return row


def retry_failed_post(session: Session, post_id: int) -> Post:
    """Release a definitive failure only when no provider object was recorded."""
    row = get_post(session, post_id)
    if row.status != "failed" or row.claim_token:
        raise ClaimConflict("Only an unclaimed failed post can be retried.", path="post_id")
    if session.scalar(select(Receipt).where(Receipt.post_id == post_id)) is not None:
        raise ClaimConflict("A post with a receipt cannot be retried.", path="post_id")
    attempt = session.scalar(
        select(PublishAttempt).where(PublishAttempt.post_id == post_id).order_by(PublishAttempt.id.desc())
    )
    if attempt is None or attempt.state != "failed" or attempt.pending_json:
        raise ClaimConflict("The failed attempt is not safe to retry.", path="post_id")
    remote = json.loads(attempt.remote_ids_json)
    provider_keys = set(remote) - {"network_started", "phase"}
    if provider_keys:
        raise ClaimConflict("The failed attempt recorded a provider object and cannot be blindly retried.", path="post_id")
    row.status = "approved"
    row.error = None
    row.notify_error = None
    row.updated_at = utcnow()
    session.commit()
    return row


def cancel_post(session: Session, post_id: int) -> Post:
    row = get_post(session, post_id)
    if row.status not in {"draft", "approved"} or row.claim_token:
        raise ClaimConflict("Only an unclaimed draft or approved post can be cancelled.", path="post_id")
    row.status = "cancelled"
    row.updated_at = utcnow()
    session.commit()
    return row


def claim_post(engine: Engine, post_id: int, *, now: datetime | None = None) -> str:
    current = now or utcnow()
    token = secrets.token_hex(24)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        result = connection.execute(
            text(
                "UPDATE posts SET claim_token=:token, claim_expires_at=:expires, "
                "status='publishing', updated_at=:now WHERE id=:id AND status='approved' "
                "AND claim_token IS NULL AND scheduled_at<=:now"
            ),
            {
                "token": token,
                "expires": current + timedelta(seconds=LEASE_SECONDS),
                "now": current,
                "id": post_id,
            },
        )
        connection.commit()
    if result.rowcount != 1:
        raise ClaimConflict("Post is not due or another worker already claimed it.", path="post_id")
    return token


def due_ids(session: Session, *, now: datetime | None = None, limit: int = 100) -> list[int]:
    if limit < 1:
        raise InvalidInput("limit must be positive.", path="limit")
    current = now or utcnow()
    return list(
        session.scalars(
            select(Post.id)
            .where(Post.status == "approved", Post.claim_token.is_(None), Post.scheduled_at <= current)
            .order_by(Post.scheduled_at, Post.id)
            .limit(limit)
        )
    )


def run_due(
    engine: Engine,
    store: Path,
    drivers: dict[str, Any],
    *,
    dry_run: bool = False,
    only_ids: list[int] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    with Session(engine) as session:
        candidates = only_ids if only_ids is not None else due_ids(session)
    results: list[dict[str, Any]] = []
    for post_id in candidates:
        try:
            token = claim_post(engine, post_id)
        except ClaimConflict as exc:
            results.append({"id": post_id, "outcome": "skipped", "error": exc.detail, "exit_code": exc.exit_code})
            continue
        try:
            receipt, transcript = _execute_claim(engine, store, drivers, post_id, token, dry_run=dry_run, sleep=sleep)
            results.append({"id": post_id, "outcome": "published", "receipt": serialize_receipt(receipt), "api_calls": transcript})
        except PostdeskError as exc:
            _record_failure(engine, store, post_id, token, exc)
            results.append({"id": post_id, "outcome": "in_doubt" if _is_in_doubt(exc) else "failed", "error": exc.detail, "exit_code": exc.exit_code})
        except Exception as exc:
            wrapped = ApiError(f"Unexpected publish failure: {exc}", definitive=False)
            _record_failure(engine, store, post_id, token, wrapped)
            results.append({"id": post_id, "outcome": "in_doubt", "error": wrapped.detail, "exit_code": wrapped.exit_code})
    return {
        "ok": not any(item["outcome"] in {"failed", "in_doubt"} for item in results),
        "due": len(candidates),
        "published": sum(item["outcome"] == "published" for item in results),
        "failed": sum(item["outcome"] == "failed" for item in results),
        "in_doubt": sum(item["outcome"] == "in_doubt" for item in results),
        "skipped": sum(item["outcome"] == "skipped" for item in results),
        "results": results,
    }


def _execute_claim(
    engine: Engine,
    store: Path,
    drivers: dict[str, Any],
    post_id: int,
    token: str,
    *,
    dry_run: bool,
    sleep: Callable[[float], None],
) -> tuple[Receipt, list[dict[str, Any]]]:
    with Session(engine, expire_on_commit=False) as session:
        post = get_post(session, post_id)
        if post.claim_token != token or post.status != "publishing":
            raise ClaimConflict("Publish lease no longer belongs to this worker.", path="claim_token")
        attempt = PublishAttempt(
            tenant=post.tenant,
            post_id=post.id,
            claim_token=token,
            external_ref=post.external_ref,
            state="started",
            remote_ids_json="{}",
            started_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(attempt)
        session.commit()
        attempt_id = attempt.id
        config = load_tenant(store, post.tenant)
        credentials = config.channel(post.channel, dry_run=dry_run)
        driver = drivers[post.channel]
        data = _post_data(post)

    def checkpoint(values: dict[str, Any]) -> None:
        with Session(engine) as checkpoint_session:
            row = checkpoint_session.get(PublishAttempt, attempt_id)
            if row is None:
                raise ClaimConflict("Publish attempt disappeared.", path="attempt")
            current = json.loads(row.remote_ids_json)
            current.update(values)
            row.remote_ids_json = json.dumps(current, sort_keys=True)
            row.updated_at = utcnow()
            checkpoint_session.commit()

    transcript: list[dict[str, Any]] = []
    ctx = _context(config, credentials, checkpoint, dry_run, transcript)
    attempt_data = AttemptData(attempt_id, data.tenant, data.external_ref or "", {})
    result = driver.publish(data, attempt_data, ctx)
    if isinstance(result, Pending):
        max_polls, interval = POLL_LIMITS.get((post.channel, post.kind), (20, 3.0))
        for poll_number in range(1, max_polls + 1):
            _save_pending(engine, attempt_id, result.handle, poll_number)
            polled = driver.poll(result, ctx)
            if isinstance(polled, Pending):
                result = polled
                if poll_number < max_polls:
                    sleep(interval)
                continue
            if isinstance(polled, Failed):
                raise ApiError(polled.detail, definitive=True)
            result = polled
            break
        else:
            raise ApiError(f"{post.channel}.{post.kind} did not finish after {max_polls} polls.", definitive=False)
        if isinstance(result, Pending):
            raise ApiError(f"{post.channel}.{post.kind} did not finish after {max_polls} polls.", definitive=False)
    receipt = _finish_publish(engine, post_id, attempt_id, token, result)
    config = load_tenant(store, receipt.tenant)
    notification_error = notify(f"Post {post_id} published on {receipt.channel}.", config.hooks)
    if notification_error:
        with Session(engine) as session:
            row = session.get(Post, post_id)
            row.notify_error = notification_error
            session.commit()
    return receipt, transcript


def _save_pending(engine: Engine, attempt_id: int, handle: dict[str, Any], poll_number: int) -> None:
    with Session(engine) as session:
        row = session.get(PublishAttempt, attempt_id)
        if row is None:
            raise ClaimConflict("Publish attempt disappeared.", path="attempt")
        row.state = "pending"
        row.pending_json = json.dumps({"handle": handle, "poll": poll_number})
        row.updated_at = utcnow()
        post = session.get(Post, row.post_id)
        post.status = "pending"
        post.claim_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
        post.updated_at = utcnow()
        session.commit()


def _finish_publish(engine: Engine, post_id: int, attempt_id: int, token: str, result: ReceiptData) -> Receipt:
    now = utcnow()
    with Session(engine, expire_on_commit=False) as session:
        post = get_post(session, post_id)
        attempt = session.get(PublishAttempt, attempt_id)
        existing = session.scalar(select(Receipt).where(Receipt.attempt_id == attempt_id))
        if existing is not None:
            return existing
        if post.claim_token != token or attempt is None:
            raise ClaimConflict("Publish lease no longer belongs to this worker.", path="claim_token")
        receipt = Receipt(
            tenant=post.tenant,
            post_id=post.id,
            attempt_id=attempt.id,
            channel=post.channel,
            external_id=result.external_id,
            permalink=result.permalink,
            raw_json=json.dumps(result.raw, sort_keys=True),
            published_at=now,
        )
        session.add(receipt)
        attempt.state = "completed"
        attempt.pending_json = None
        attempt.completed_at = now
        attempt.updated_at = now
        post.status = "published"
        post.claim_token = None
        post.claim_expires_at = None
        post.error = None
        post.updated_at = now
        session.commit()
        return receipt


def _record_failure(engine: Engine, store: Path, post_id: int, token: str, exc: PostdeskError) -> None:
    with Session(engine) as session:
        post = get_post(session, post_id)
        attempt = session.scalar(
            select(PublishAttempt).where(PublishAttempt.post_id == post_id).order_by(PublishAttempt.id.desc())
        )
        remote = json.loads(attempt.remote_ids_json) if attempt else {}
        in_doubt = isinstance(exc, ApiError) and not exc.definitive and bool(remote.get("network_started"))
        post.status = "in_doubt" if in_doubt else "failed"
        post.error = exc.detail[:1000]
        post.updated_at = utcnow()
        if not in_doubt:
            post.claim_token = None
            post.claim_expires_at = None
        if attempt:
            attempt.state = "unknown" if in_doubt else "failed"
            attempt.error = exc.detail[:1000]
            attempt.updated_at = utcnow()
            if not in_doubt:
                attempt.completed_at = utcnow()
        session.commit()
        config = load_tenant(store, post.tenant)
        message = f"Post {post_id} is in doubt and requires reconcile." if in_doubt else f"Post {post_id} failed to publish."
        notify_error = notify(message, config.hooks)
        if notify_error:
            post.notify_error = notify_error
            session.commit()


def _is_in_doubt(exc: PostdeskError) -> bool:
    return isinstance(exc, ApiError) and not exc.definitive


def reconcile(engine: Engine, store: Path, drivers: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    with Session(engine) as session:
        candidates = list(
            session.scalars(
                select(Post).where(
                    (Post.status == "in_doubt")
                    | (
                        Post.status.in_(["publishing", "pending"])
                        & (Post.claim_expires_at.is_not(None))
                        & (Post.claim_expires_at <= now)
                    )
                )
            )
        )
        ids = [row.id for row in candidates]
    results: list[dict[str, Any]] = []
    for post_id in ids:
        with Session(engine) as session:
            post = get_post(session, post_id)
            attempt = session.scalar(select(PublishAttempt).where(PublishAttempt.post_id == post_id).order_by(PublishAttempt.id.desc()))
            if attempt is None:
                post.status = "approved"
                post.claim_token = None
                post.claim_expires_at = None
                post.updated_at = utcnow()
                session.commit()
                results.append({"id": post_id, "outcome": "released"})
                continue
            config = load_tenant(store, post.tenant)
            credentials = config.channel(post.channel, dry_run=False)
            driver = drivers[post.channel]
            attempt_data = AttemptData(attempt.id, attempt.tenant, attempt.external_ref, json.loads(attempt.remote_ids_json))
            ctx = _context(config, credentials, lambda _: None, False, [])
            outcome = driver.lookup(attempt_data, ctx)
            if isinstance(outcome, Found):
                receipt = _finish_publish(engine, post.id, attempt.id, post.claim_token or attempt.claim_token, outcome.receipt)
                results.append({"id": post_id, "outcome": "found", "receipt": serialize_receipt(receipt)})
            elif isinstance(outcome, NotFound):
                post.status = "approved"
                post.claim_token = None
                post.claim_expires_at = None
                post.error = None
                post.updated_at = utcnow()
                attempt.state = "not_found"
                attempt.completed_at = utcnow()
                attempt.updated_at = utcnow()
                session.commit()
                results.append({"id": post_id, "outcome": "released"})
            elif isinstance(outcome, Unknown):
                should_notify = post.status != "in_doubt" or post.error != outcome.detail
                post.status = "in_doubt"
                post.error = outcome.detail
                post.updated_at = utcnow()
                attempt.state = "unknown"
                attempt.error = outcome.detail
                attempt.updated_at = utcnow()
                session.commit()
                if should_notify:
                    notification_error = notify(f"Post {post_id} is in doubt.", config.hooks)
                    if notification_error:
                        post.notify_error = notification_error
                        session.commit()
                results.append({"id": post_id, "outcome": "in_doubt", "detail": outcome.detail})
    return {"ok": True, "checked": len(ids), "results": results}


def edit_receipt(engine: Engine, store: Path, drivers: dict[str, Any], receipt_id: int, caption: str) -> Receipt:
    with Session(engine, expire_on_commit=False) as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            raise InvalidInput(f"Receipt {receipt_id} was not found.", path="receipt")
        config = load_tenant(store, receipt.tenant)
        credentials = config.channel(receipt.channel, dry_run=False)
        driver = drivers[receipt.channel]
        if not driver.capabilities().features.get("edit"):
            raise InvalidInput(f"{receipt.channel}.edit is not supported.", path=f"capabilities.{receipt.channel}.features.edit")
        data = ReceiptData(receipt.external_id, receipt.permalink, json.loads(receipt.raw_json))
        changed = driver.edit(data, caption, _context(config, credentials, lambda _: None, False, []))
        receipt.raw_json = json.dumps(changed.raw, sort_keys=True)
        session.commit()
        return receipt


def delete_receipt(engine: Engine, store: Path, drivers: dict[str, Any], receipt_id: int) -> Receipt:
    with Session(engine, expire_on_commit=False) as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            raise InvalidInput(f"Receipt {receipt_id} was not found.", path="receipt")
        config = load_tenant(store, receipt.tenant)
        credentials = config.channel(receipt.channel, dry_run=False)
        driver = drivers[receipt.channel]
        if not driver.capabilities().features.get("delete"):
            raise InvalidInput(f"{receipt.channel}.delete is not supported.", path=f"capabilities.{receipt.channel}.features.delete")
        driver.delete(ReceiptData(receipt.external_id, receipt.permalink, json.loads(receipt.raw_json)), _context(config, credentials, lambda _: None, False, []))
        receipt.deleted_at = utcnow()
        session.commit()
        return receipt


def pull_insights(engine: Engine, store: Path, drivers: dict[str, Any], tenant: str, since: str | None) -> list[Metric]:
    since_at = parse_time(since) if since else None
    created: list[Metric] = []
    with Session(engine, expire_on_commit=False) as session:
        query = select(Receipt).where(Receipt.tenant == tenant, Receipt.deleted_at.is_(None))
        if since_at:
            query = query.where(Receipt.published_at >= since_at)
        receipts = list(session.scalars(query.order_by(Receipt.channel, Receipt.id)))
        config = load_tenant(store, tenant)
        for channel in sorted({row.channel for row in receipts}):
            channel_rows = [row for row in receipts if row.channel == channel]
            credentials = config.channel(channel, dry_run=False)
            driver = drivers[channel]
            values = driver.insights(
                [ReceiptData(row.external_id, row.permalink, json.loads(row.raw_json)) for row in channel_rows],
                _context(config, credentials, lambda _: None, False, []),
            )
            by_external = {row.external_id: row for row in channel_rows}
            for value in values:
                receipt = by_external.get(value.receipt_external_id)
                if receipt is None:
                    continue
                metric = Metric(
                    tenant=tenant,
                    receipt_id=receipt.id,
                    channel=channel,
                    name=value.name,
                    value_json=json.dumps(value.value, sort_keys=True),
                    raw_json=json.dumps(value.raw, sort_keys=True),
                    fetched_at=value.fetched_at,
                )
                session.add(metric)
                created.append(metric)
        session.commit()
    return created


def serialize_metric(row: Metric) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant": row.tenant,
        "receipt_id": row.receipt_id,
        "channel": row.channel,
        "name": row.name,
        "value": json.loads(row.value_json),
        "raw": json.loads(row.raw_json),
        "fetched_at": _iso(row.fetched_at),
    }


def _context(
    config: TenantConfig,
    credentials: dict[str, str],
    checkpoint: Callable[[dict[str, Any]], None],
    dry_run: bool,
    transcript: list[dict[str, Any]],
) -> Ctx:
    return Ctx(
        credentials=credentials,
        media_url=lambda value: resolve_media(value, config.hooks),
        checkpoint=checkpoint,
        dry_run=dry_run,
        timeout_seconds=hook_timeout(config.hooks),
        transcript=transcript,
    )


def _post_data(post: Post) -> PostData:
    return PostData(
        id=post.id,
        tenant=post.tenant,
        channel=post.channel,
        kind=post.kind,
        media=json.loads(post.media_json),
        caption=post.caption,
        first_comment=post.first_comment,
        collaborators=json.loads(post.collaborators_json),
        link=post.link,
        external_ref=post.external_ref,
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
