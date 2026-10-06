from __future__ import annotations

import json
from pathlib import Path

import pytest

from postdesk.drivers.facebook import FacebookDriver
from postdesk.drivers.instagram import InstagramDriver
from postdesk.errors import ApiError, ConfigError
from postdesk.http import UrlLibTransport
from postdesk.types import AttemptData, Ctx, Found, NotFound, Pending, PostData, ReceiptData, Unknown

from .helpers import FixtureTransport

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str, kind: str):
    return json.loads((FIXTURES / name / f"{kind}.json").read_text(encoding="utf-8"))


def context(*, dry_run: bool = False, channel: str = "instagram") -> tuple[Ctx, list[dict]]:
    checkpoints: list[dict] = []
    credentials = (
        {"ig_user_id": "ig-user", "expected_username": "fixture-account", "token": "fixture-token"}
        if channel == "instagram"
        else {"page_id": "fb-page", "token": "fixture-token"}
    )
    return (
        Ctx(
            credentials=credentials,
            media_url=lambda value: value,
            checkpoint=lambda value: checkpoints.append(value),
            dry_run=dry_run,
            timeout_seconds=1,
        ),
        checkpoints,
    )


def post(channel: str, kind: str) -> PostData:
    if channel == "instagram":
        media = {
            "photo": ["https://media.example/photo.jpg"],
            "carousel": ["https://media.example/one.jpg", "https://media.example/two.jpg"],
            "reel": ["https://media.example/reel.mp4"],
            "story": ["https://media.example/story.jpg"],
        }[kind]
        return PostData(
            1,
            "demo",
            channel,
            kind,
            media,
            None if kind == "story" else "Fixture caption",
            external_ref=f"ig-{kind}",
        )
    media = {
        "text": [],
        "link": [],
        "photo": ["https://media.example/photo.jpg"],
        "album": ["https://media.example/one.jpg", "https://media.example/two.jpg"],
        "video": ["https://media.example/video.mp4"],
    }[kind]
    return PostData(
        1,
        "demo",
        channel,
        kind,
        media,
        "Fixture caption",
        link="https://example.com" if kind == "link" else None,
        external_ref=f"fb-{kind}",
    )


def finish(driver, result, ctx):
    for _ in range(10):
        if not isinstance(result, Pending):
            return result
        result = driver.poll(result, ctx)
    raise AssertionError("fixture did not finish")


@pytest.mark.parametrize("kind", ["photo", "carousel", "reel", "story"])
def test_instagram_contract_from_recorded_fixtures(kind):
    transport = FixtureTransport([{"user_id": "ig-user", "username": "fixture-account"}, *fixture("instagram", kind)])
    driver = InstagramDriver(transport)
    ctx, checkpoints = context(channel="instagram")
    item = post("instagram", kind)
    assert driver.validate(item) == []
    result = finish(driver, driver.publish(item, AttemptData(1, "demo", item.external_ref, {}), ctx), ctx)
    assert isinstance(result, ReceiptData)
    assert result.external_id == f"ig-media-{kind}"
    assert any("network_started" in value for value in checkpoints)
    assert any("creation_id" in value for value in checkpoints)
    transport.assert_consumed()


def test_instagram_refuses_mismatched_account_before_creating_media():
    transport = FixtureTransport([{"user_id": "ig-user", "username": "wrong-account"}])
    driver = InstagramDriver(transport)
    ctx, checkpoints = context(channel="instagram")
    item = post("instagram", "photo")

    with pytest.raises(ConfigError, match="belongs to @wrong-account, expected @fixture-account"):
        driver.publish(item, AttemptData(1, "demo", item.external_ref, {}), ctx)

    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith("/me")
    assert checkpoints == []
    transport.assert_consumed()


def test_instagram_system_user_token_uses_the_facebook_graph_host():
    transport = FixtureTransport([{"id": "ig-user", "username": "fixture-account"}, *fixture("instagram", "photo")])
    driver = InstagramDriver(transport)
    ctx, checkpoints = context(channel="instagram")
    ctx.credentials["graph_host"] = "graph.facebook.com"
    item = post("instagram", "photo")
    result = finish(driver, driver.publish(item, AttemptData(1, "demo", item.external_ref, {}), ctx), ctx)
    assert isinstance(result, ReceiptData)
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"] == "https://graph.facebook.com/v23.0/ig-user"
    assert all(call["url"].startswith("https://graph.facebook.com/v23.0/") for call in transport.calls)
    transport.assert_consumed()

    dry_ctx, _ = context(dry_run=True, channel="instagram")
    dry_ctx.credentials["graph_host"] = "graph.facebook.com"
    planned = InstagramDriver(FixtureTransport([])).publish(item, AttemptData(2, "demo", item.external_ref, {}), dry_ctx)
    assert all(call["url"].startswith("https://graph.facebook.com/v23.0/") for call in planned.raw["api_calls"])

    bad_ctx, _ = context(channel="instagram")
    bad_ctx.credentials["graph_host"] = "graph.example.com"
    with pytest.raises(ConfigError, match="graph_host"):
        InstagramDriver(FixtureTransport([])).publish(item, AttemptData(3, "demo", item.external_ref, {}), bad_ctx)


@pytest.mark.parametrize("kind", ["text", "link", "photo", "album", "video"])
def test_facebook_contract_from_recorded_fixtures(kind):
    transport = FixtureTransport(fixture("facebook", kind))
    driver = FacebookDriver(transport)
    ctx, checkpoints = context(channel="facebook")
    item = post("facebook", kind)
    assert driver.validate(item) == []
    result = finish(driver, driver.publish(item, AttemptData(1, "demo", item.external_ref, {}), ctx), ctx)
    assert isinstance(result, ReceiptData)
    expected = "fb-album" if kind == "album" else f"fb-{kind}"
    assert result.external_id == expected
    assert any("network_started" in value for value in checkpoints)
    transport.assert_consumed()
    assert all("scheduled_publish_time" not in call["parameters"] for call in transport.calls)


@pytest.mark.parametrize(
    ("driver", "channel", "kind"),
    [
        *[(InstagramDriver(), "instagram", kind) for kind in ("photo", "carousel", "reel", "story")],
        *[(FacebookDriver(), "facebook", kind) for kind in ("text", "link", "photo", "album", "video")],
    ],
)
def test_dry_run_transcripts_cover_every_kind(driver, channel, kind):
    ctx, checkpoints = context(dry_run=True, channel=channel)
    item = post(channel, kind)
    result = driver.publish(item, AttemptData(1, "demo", item.external_ref, {}), ctx)
    assert isinstance(result, ReceiptData)
    assert result.raw["dry_run"] is True
    assert result.raw["api_calls"]
    assert checkpoints == []
    serialized = json.dumps(result.raw)
    assert "fixture-token" not in serialized
    assert "<token>" in serialized


def test_api_error_keeps_detail_and_network_exit_code():
    error = ApiError("Recorded permission failure", path="fixture", definitive=True)
    transport = FixtureTransport([error])
    driver = FacebookDriver(transport)
    ctx, _ = context(channel="facebook")
    item = post("facebook", "text")
    with pytest.raises(ApiError, match="Recorded permission failure") as caught:
        driver.publish(item, AttemptData(1, "demo", item.external_ref, {}), ctx)
    assert caught.value.exit_code == 4
    assert caught.value.detail == "Recorded permission failure"


def test_recorded_api_error_payload_maps_without_network(monkeypatch):
    raw = (FIXTURES / "facebook" / "error.json").read_bytes()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return raw

    monkeypatch.setattr("postdesk.http.urlopen", lambda *_args, **_kwargs: Response())
    with pytest.raises(ApiError, match="Recorded permission failure") as caught:
        UrlLibTransport().request(
            "GET",
            "https://graph.facebook.com/v23.0/fixture",
            {"access_token": "fixture-token"},
            timeout=1,
        )
    assert caught.value.exit_code == 4
    assert caught.value.definitive is True


def test_capability_validation_is_per_kind():
    instagram = InstagramDriver()
    facebook = FacebookDriver()
    bad_story = PostData(1, "demo", "instagram", "story", ["https://media.example/a.jpg"], "not allowed")
    bad_link = PostData(2, "demo", "facebook", "link", ["https://media.example/a.jpg"], "caption")
    assert "caption is not supported" in " ".join(instagram.validate(bad_story))
    problems = " ".join(facebook.validate(bad_link))
    assert "media requires 0..0" in problems
    assert "link is required" in problems


def test_lookup_fails_closed_unless_not_found_is_explicit():
    ctx, _ = context(channel="facebook")
    attempt = AttemptData(1, "demo", "lookup", {"network_started": True, "post_id": "fb-post"})

    permission = FacebookDriver(
        FixtureTransport([ApiError("permission denied", definitive=True)])
    ).lookup(attempt, ctx)
    missing = FacebookDriver(
        FixtureTransport([ApiError("object missing", definitive=True, not_found=True)])
    ).lookup(attempt, ctx)
    no_request = FacebookDriver().lookup(
        AttemptData(2, "demo", "clean", {}), ctx
    )

    assert isinstance(permission, Unknown)
    assert isinstance(missing, NotFound)
    assert isinstance(no_request, NotFound)


def test_facebook_edit_delete_and_raw_insights_contract():
    transport = FixtureTransport(
        [
            {"success": True},
            {"success": True},
            {
                "data": [
                    {
                        "name": "post_clicks_by_type",
                        "values": [{"value": {"link clicks": 3}}],
                    }
                ]
            },
        ]
    )
    driver = FacebookDriver(transport)
    ctx, _ = context(channel="facebook")
    receipt = ReceiptData("fb-post", "https://facebook.example/post", {"source": "fixture"})
    edited = driver.edit(receipt, "Updated caption", ctx)
    driver.delete(receipt, ctx)
    metrics = driver.insights([receipt], ctx)

    assert edited.external_id == "fb-post"
    assert metrics[0].name == "post_clicks_by_type"
    assert metrics[0].value == [{"value": {"link clicks": 3}}]
    assert metrics[0].raw == {
        "name": "post_clicks_by_type",
        "values": [{"value": {"link clicks": 3}}],
    }
    assert metrics[0].receipt_external_id == "fb-post"
    transport.assert_consumed()


def test_instagram_raw_insights_contract():
    raw = {"name": "reach", "values": [{"value": 42}]}
    driver = InstagramDriver(FixtureTransport([{"data": [raw]}]))
    ctx, _ = context(channel="instagram")
    metrics = driver.insights([ReceiptData("ig-post")], ctx)
    assert metrics[0].raw == raw
    assert metrics[0].receipt_external_id == "ig-post"
