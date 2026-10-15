from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest
from sqlalchemy import text

from postdesk.drivers.youtube import YouTubeDriver, title_for
from postdesk.errors import ApiError, ConfigError
from postdesk.http import HttpResponse
from postdesk.store import initialize
from postdesk.types import AttemptData, Ctx, Found, NotFound, Pending, PostData, ReceiptData, Unknown

FIXTURE = Path(__file__).parent / "fixtures" / "youtube" / "video.json"


class JsonFixtureTransport:
    """Replays recorded Google responses in order and records every request."""

    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def send(self, method, url, *, headers, query=None, json_body=None, form=None, data=None, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "query": query, "json": json_body, "form": form, "data": data})
        if not self.responses:
            raise AssertionError(f"No fixture response left for {method} {url}")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return HttpResponse(response.get("status", 200), {k.lower(): v for k, v in response.get("headers", {}).items()}, response.get("payload", {}))

    def fetch_file(self, url, destination, *, timeout):
        raise AssertionError("fixtures use local files")

    def assert_consumed(self):
        assert not self.responses


def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def context(*, dry_run: bool = False, playlist: str = "PL-fixture") -> tuple[Ctx, list[dict]]:
    checkpoints: list[dict] = []
    credentials = {
        "channel_id": "UC-fixture",
        "expected_handle": "@Fixture",
        "token": "fixture-refresh",
        "token_env": "POSTDESK_DEMO_YOUTUBE_TOKEN",
        "client_id_env": "FIXTURE_CLIENT_ID",
        "client_secret_env": "FIXTURE_CLIENT_SECRET",
        "privacy": "public",
    }
    if playlist:
        credentials["playlist_id"] = playlist
    return (
        Ctx(credentials=credentials, media_url=lambda value: value, checkpoint=lambda value: checkpoints.append(value), dry_run=dry_run, timeout_seconds=1),
        checkpoints,
    )


@pytest.fixture
def client_env(monkeypatch):
    monkeypatch.setenv("FIXTURE_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("FIXTURE_CLIENT_SECRET", "fixture-secret")


def video_post(tmp_path, *, title="Fixture title", caption="Fixture caption\n#tags", first_comment="First!") -> PostData:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"abcdefgh")
    return PostData(1, "demo", "youtube", "video", [str(path)], caption, first_comment=first_comment, external_ref="yt-video", title=title)


def test_upload_checkpoints_session_before_bytes_then_polls_until_processed(tmp_path, client_env):
    transport = JsonFixtureTransport(recorded())
    driver = YouTubeDriver(transport, chunk_bytes=4)
    ctx, checkpoints = context()
    item = video_post(tmp_path)
    assert driver.validate(item) == []

    result = driver.publish(item, AttemptData(1, "demo", "yt-video", {}), ctx)
    assert isinstance(result, Pending)
    assert result.handle == {"video_id": "yt-video", "playlist_item_id": "PLI-1", "first_comment_id": "comment-1"}
    assert isinstance(driver.poll(result, ctx), Pending)
    receipt = driver.poll(result, ctx)
    assert isinstance(receipt, ReceiptData)
    assert (receipt.external_id, receipt.permalink) == ("yt-video", "https://www.youtube.com/watch?v=yt-video")
    assert receipt.raw["privacy"] == "public"
    transport.assert_consumed()

    methods = [(call["method"], call["url"]) for call in transport.calls]
    assert methods[:3] == [
        ("POST", "https://oauth2.googleapis.com/token"),
        ("GET", "https://www.googleapis.com/youtube/v3/channels"),
        ("POST", "https://www.googleapis.com/upload/youtube/v3/videos"),
    ]
    puts = [call for call in transport.calls if call["method"] == "PUT"]
    assert [call["headers"]["Content-Range"] for call in puts] == ["bytes 0-3/8", "bytes 4-7/8"]
    assert b"".join(call["data"] for call in puts) == b"abcdefgh"
    assert transport.calls[2]["json"]["snippet"] == {"title": "Fixture title", "description": "Fixture caption\n#tags", "categoryId": "22"}
    assert transport.calls[2]["headers"]["X-Upload-Content-Length"] == "8"
    # The session is durable before the first byte leaves, so a lost response can be reconciled.
    session_index = next(i for i, value in enumerate(checkpoints) if "upload_session" in value)
    video_index = next(i for i, value in enumerate(checkpoints) if "video_id" in value)
    assert checkpoints[0]["network_started"] is True and checkpoints[0]["title"] == "Fixture title"
    assert session_index < video_index
    assert checkpoints[session_index] == {"upload_session": "https://www.googleapis.com/upload/session/fixture", "upload_size": 8}
    assert all("fixture-access" not in json.dumps(value) for value in checkpoints)
    # Only one token exchange for the whole publish + polls.
    assert sum(1 for method, url in methods if url.endswith("/token")) == 1


def test_mismatched_channel_blocks_before_any_upload(tmp_path, client_env):
    transport = JsonFixtureTransport(
        [
            {"payload": {"access_token": "a"}},
            {"payload": {"items": [{"id": "UC-other", "snippet": {"customUrl": "@someone"}}]}},
        ]
    )
    ctx, checkpoints = context()
    with pytest.raises(ConfigError, match="belongs to channel UC-other .*expected UC-fixture"):
        YouTubeDriver(transport).publish(video_post(tmp_path), AttemptData(1, "demo", "yt-video", {}), ctx)
    assert checkpoints == []
    transport.assert_consumed()


def test_dry_run_plans_without_network_and_without_client_secrets(tmp_path):
    transport = JsonFixtureTransport([])
    ctx, checkpoints = context(dry_run=True)
    receipt = YouTubeDriver(transport).publish(video_post(tmp_path), AttemptData(1, "demo", "yt-video", {}), ctx)
    assert receipt.external_id == "dry-run:youtube:yt-video"
    urls = [call["url"] for call in receipt.raw["api_calls"]]
    assert urls[0] == "https://oauth2.googleapis.com/token"
    assert "{upload_session}" in urls
    assert any(call["url"].endswith("/playlistItems") for call in receipt.raw["api_calls"])
    assert "fixture-refresh" not in json.dumps(receipt.raw)
    assert transport.calls == [] and checkpoints == []


def test_validate_needs_a_title_and_rejects_unsupported_fields(tmp_path):
    driver = YouTubeDriver(JsonFixtureTransport([]))
    item = video_post(tmp_path, title=None, caption="#only #tags")
    item.collaborators = ["friend"]
    problems = driver.validate(item)
    assert any("title is required" in problem for problem in problems)
    assert any("collaborators" in problem for problem in problems)
    assert driver.validate(video_post(tmp_path, title=None, caption="Kiln opening day 3 #fire\nmore")) == []
    assert title_for(video_post(tmp_path, title=None, caption="#fire Kiln opening — day 3\nmore")) == "Kiln opening — day 3"


@pytest.mark.parametrize(
    ("remote", "responses", "expected"),
    [
        # A checkpointed video id is simply looked up.
        ({"video_id": "yt-video"}, [{"payload": {"access_token": "a"}}, {"payload": {"items": [{"id": "yt-video", "status": {"privacyStatus": "private"}}]}}], Found),
        # The upload session finished on Google's side although our response was lost.
        ({"upload_session": "https://www.googleapis.com/upload/session/x", "upload_size": 8, "network_started": True}, [{"payload": {"access_token": "a"}}, {"status": 200, "payload": {"id": "yt-late"}}, {"payload": {"items": [{"id": "yt-late", "status": {}}]}}], Found),
        # The session never completed: nothing was published, safe to release.
        ({"upload_session": "https://www.googleapis.com/upload/session/x", "upload_size": 8, "network_started": True}, [{"payload": {"access_token": "a"}}, {"status": 308, "payload": {}}], NotFound),
        # No session recorded: scan the newest uploads for the attempt's title.
        (
            {"network_started": True, "title": "Fixture title", "started_at": "2026-10-15T18:00:00+00:00"},
            [
                {"payload": {"access_token": "a"}},
                {"payload": {"items": [{"id": "UC-fixture", "snippet": {"customUrl": "@fixture"}, "contentDetails": {"relatedPlaylists": {"uploads": "UU-fixture"}}}]}},
                {"payload": {"items": [{"snippet": {"title": "Older", "publishedAt": "2026-10-15T17:00:00Z", "resourceId": {"videoId": "old"}}}, {"snippet": {"title": "Fixture title", "publishedAt": "2026-10-15T18:00:05Z", "resourceId": {"videoId": "yt-scan"}}}]}},
                {"payload": {"items": [{"id": "yt-scan", "status": {}}]}},
            ],
            Found,
        ),
        # Nothing reached the network.
        ({}, [], NotFound),
        # Google is unreachable: hold the post in doubt rather than guess.
        ({"video_id": "yt-video"}, [ApiError("API unavailable: timeout", definitive=False)], Unknown),
    ],
)
def test_lookup_reconciles_from_the_strongest_remote_handle(remote, responses, expected, client_env):
    transport = JsonFixtureTransport(responses)
    ctx, _checkpoints = context()
    outcome = YouTubeDriver(transport).lookup(AttemptData(1, "demo", "yt-video", remote), ctx)
    assert isinstance(outcome, expected)
    if isinstance(outcome, Found):
        assert outcome.receipt.permalink.startswith("https://www.youtube.com/watch?v=")
    transport.assert_consumed()
    probes = [call for call in transport.calls if call["method"] == "PUT"]
    assert all(call["headers"]["Content-Range"] == "bytes */8" and call["data"] == b"" for call in probes)


def test_store_adds_the_title_column_to_databases_created_before_it(tmp_path):
    path = tmp_path / "old.sqlite3"
    engine = initialize(path)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE posts DROP COLUMN title")
        assert "title" not in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(posts)")}
    engine.dispose()
    upgraded = initialize(path)
    with upgraded.connect() as connection:
        assert "title" in {row[1] for row in connection.execute(text("PRAGMA table_info(posts)"))}
