from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from postdesk.config import DEMO_CONFIG
from postdesk.core import add_post, approve_post, reconcile, retry_failed_post, run_due, utcnow
from postdesk.errors import ClaimConflict
from postdesk.models import Post, PublishAttempt, Receipt
from postdesk.store import initialize
from postdesk.types import (
    Capabilities,
    Found,
    KindCapability,
    NotFound,
    ReceiptData,
    Unknown,
)


class FakeDriver:
    id = "fake"

    def __init__(self, engine=None, lookup_result=None):
        self.engine = engine
        self.lookup_result = lookup_result or NotFound()
        self.publish_calls = 0
        self.lock = threading.Lock()

    def capabilities(self):
        return Capabilities({"text": KindCapability(0, 0, (), "required")}, {"edit": False, "delete": False}, ())

    def validate(self, post):
        return [] if post.kind == "text" and post.caption else ["fake.text.caption is required."]

    def publish(self, post, attempt, ctx):
        with Session(self.engine) as session:
            assert session.get(PublishAttempt, attempt.id) is not None
        with self.lock:
            self.publish_calls += 1
        ctx.checkpoint({"network_started": True, "post_id": f"fake-{post.id}"})
        return ReceiptData(f"fake-{post.id}", raw={"fixture": True})

    def poll(self, pending, ctx):
        raise AssertionError("fake publishing is synchronous")

    def lookup(self, attempt, ctx):
        return self.lookup_result


def make_store(tmp_path, *, notify_cmd=None):
    store = tmp_path / "postdesk.sqlite3"
    engine = initialize(store)
    config = DEMO_CONFIG + "\n[channels.fake]\ntoken_env = \"POSTDESK_FAKE_TOKEN\"\n"
    if notify_cmd is not None:
        before, _, after = config.partition("notify_cmd = []")
        config = before + f"notify_cmd = {json.dumps(notify_cmd)}" + after
    path = tmp_path / "tenants" / "demo" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(config, encoding="utf-8")
    return store, engine


def enqueue(session, store, driver, ref="parallel-ref"):
    row, created = add_post(
        session,
        store,
        {"fake": driver},
        tenant="demo",
        channel="fake",
        kind="text",
        media=[],
        caption="Fixture post",
        first_comment=None,
        collaborators=[],
        link=None,
        at="2020-01-01T00:00:00Z",
        external_ref=ref,
    )
    assert created
    approve_post(session, row.id)
    return row.id


def test_parallel_workers_claim_once_and_reconcile_is_idempotent(tmp_path):
    store, engine = make_store(tmp_path)
    driver = FakeDriver(engine)
    with Session(engine, expire_on_commit=False) as session:
        post_id = enqueue(session, store, driver)

    def worker():
        return run_due(engine, store, {"fake": driver}, dry_run=True, sleep=lambda _: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))

    assert driver.publish_calls == 1
    assert sum(item["published"] for item in results) == 1
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Receipt)) == 1
        assert session.scalar(select(func.count()).select_from(PublishAttempt)) == 1
        assert session.get(Post, post_id).status == "published"
    assert reconcile(engine, store, {"fake": driver}) == {"ok": True, "checked": 0, "results": []}
    assert reconcile(engine, store, {"fake": driver}) == {"ok": True, "checked": 0, "results": []}


def test_retry_failed_post_requires_no_provider_object(tmp_path):
    store, engine = make_store(tmp_path)
    driver = FakeDriver(engine)
    with Session(engine, expire_on_commit=False) as session:
        post_id = enqueue(session, store, driver)
        row = session.get(Post, post_id)
        row.status = "failed"
        row.error = "media fetch failed"
        attempt = PublishAttempt(
            tenant="demo",
            post_id=post_id,
            claim_token="failed-attempt",
            external_ref=row.external_ref,
            state="failed",
            remote_ids_json=json.dumps({"network_started": True, "phase": "container"}),
            pending_json=None,
            error=row.error,
            started_at=utcnow(),
            updated_at=utcnow(),
            completed_at=utcnow(),
        )
        session.add(attempt)
        session.commit()
        retried = retry_failed_post(session, post_id)
        assert retried.status == "approved"
        assert retried.error is None

        retried.status = "failed"
        attempt.remote_ids_json = json.dumps({"network_started": True, "creation_id": "provider-1"})
        session.commit()
        with pytest.raises(ClaimConflict, match="provider object"):
            retry_failed_post(session, post_id)


def test_external_ref_is_idempotent(tmp_path):
    store, engine = make_store(tmp_path)
    driver = FakeDriver(engine)
    kwargs = dict(
        tenant="demo",
        channel="fake",
        kind="text",
        media=[],
        caption="Fixture post",
        first_comment=None,
        collaborators=[],
        link=None,
        at=None,
        external_ref="same-ref",
    )
    with Session(engine) as session:
        first, created = add_post(session, store, {"fake": driver}, **kwargs)
        second, created_again = add_post(session, store, {"fake": driver}, **kwargs)
        assert created is True
        assert created_again is False
        assert first.id == second.id


def test_notify_failure_never_rolls_back_publish(tmp_path):
    store, engine = make_store(tmp_path, notify_cmd=["/missing/postdesk-notifier", "{message}"])
    driver = FakeDriver(engine)
    with Session(engine, expire_on_commit=False) as session:
        post_id = enqueue(session, store, driver, ref="notify-ref")
    result = run_due(engine, store, {"fake": driver}, dry_run=True, sleep=lambda _: None)
    assert result["published"] == 1
    with Session(engine) as session:
        row = session.get(Post, post_id)
        assert row.status == "published"
        assert "notify_cmd failed" in row.notify_error


def test_reconcile_unknown_holds_in_doubt(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTDESK_FAKE_TOKEN", "fixture-token")
    store, engine = make_store(tmp_path)
    driver = FakeDriver(engine, Unknown("fixture lookup was inconclusive"))
    with Session(engine, expire_on_commit=False) as session:
        post_id = enqueue(session, store, driver, ref="unknown-ref")
        post = session.get(Post, post_id)
        post.status = "in_doubt"
        post.claim_token = "lease"
        post.claim_expires_at = utcnow() - timedelta(seconds=1)
        attempt = PublishAttempt(
            tenant="demo",
            post_id=post_id,
            claim_token="lease",
            external_ref="unknown-ref",
            state="unknown",
            remote_ids_json=json.dumps({"network_started": True}),
            started_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(attempt)
        session.commit()
    result = reconcile(engine, store, {"fake": driver})
    assert result["results"] == [{"id": post_id, "outcome": "in_doubt", "detail": "fixture lookup was inconclusive"}]
    with Session(engine) as session:
        assert session.get(Post, post_id).status == "in_doubt"
