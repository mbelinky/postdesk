from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..errors import ApiError, ConfigError, InvalidInput
from ..http import Transport, UrlLibTransport
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
from .common import common_validation, media_type

BASE_URL = "https://graph.instagram.com/v23.0"
FACEBOOK_HOST = "graph.facebook.com"
INSTAGRAM_HOST = "graph.instagram.com"


def graph_host(credentials: dict[str, str]) -> str:
    """Tenant-selected Graph host. Instagram-login tokens (IGA…) work only on
    graph.instagram.com; Meta system-user or Facebook-login tokens (EAA…) work
    only on graph.facebook.com against the same Instagram user id."""
    host = str(credentials.get("graph_host") or "").strip().lower().removeprefix("https://").rstrip("/")
    if not host:
        return INSTAGRAM_HOST
    if host not in (INSTAGRAM_HOST, FACEBOOK_HOST):
        raise ConfigError(
            f"graph_host must be {INSTAGRAM_HOST} or {FACEBOOK_HOST}.",
            path="channels.instagram.graph_host",
        )
    return host
METRICS = ("views", "reach", "likes", "comments", "saves")


class InstagramDriver:
    id = "instagram"

    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or UrlLibTransport()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kinds={
                "photo": KindCapability(1, 1, ("image",), "required", async_publish=True),
                "carousel": KindCapability(2, 10, ("image", "video"), "required", async_publish=True),
                "reel": KindCapability(1, 1, ("video",), "required", async_publish=True),
                "story": KindCapability(1, 1, ("image", "video"), "forbidden", async_publish=True),
            },
            features={
                "first_comment": True,
                "collaborators": True,
                "edit": False,
                "delete": False,
                "publishing_quota_check": True,
                "location": False,
                "music": False,
                "pinning": False,
            },
            metric_names=METRICS,
        )

    def validate(self, post: PostData) -> list[str]:
        capability = self.capabilities().kinds.get(post.kind)
        if capability is None:
            return [f"instagram.kind {post.kind!r} is not supported."]
        problems = common_validation(post, capability)
        if post.kind == "story" and (post.first_comment or post.collaborators):
            problems.append("instagram.story does not support first comments or collaborators.")
        return problems

    def _call(self, method: str, path: str, parameters: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
        values = {**parameters, "access_token": ctx.credentials["token"]}
        url = f"https://{graph_host(ctx.credentials)}/v23.0/{path.lstrip('/')}"
        return self.transport.request(method, url, values, timeout=ctx.timeout_seconds)

    def _planned(self, method: str, path: str, parameters: dict[str, Any], result: str | None = None, host: str = INSTAGRAM_HOST) -> dict[str, Any]:
        call = {
            "method": method,
            "url": f"https://{host}/v23.0/{path.lstrip('/')}",
            "parameters": {**parameters, "access_token": "<token>"},
        }
        if result:
            call["result"] = result
        return call

    def _verify_account(self, ctx: Ctx, ig_user_id: str) -> None:
        expected_username = ctx.credentials.get("expected_username", "").strip().lstrip("@").casefold()
        if not expected_username:
            raise ConfigError("expected_username is required.", path="channels.instagram.expected_username")
        if graph_host(ctx.credentials) == FACEBOOK_HOST:
            # A system-user token's /me is the system user, so the Instagram
            # account is read through its own node instead.
            identity = self._call("GET", ig_user_id, {"fields": "id,username"}, ctx)
        else:
            identity = self._call("GET", "me", {"fields": "id,user_id,username"}, ctx)
        actual_username = str(identity.get("username") or "").strip().lstrip("@").casefold()
        actual_ids = {
            str(identity.get(key) or "").strip()
            for key in ("id", "user_id")
            if str(identity.get(key) or "").strip()
        }
        if actual_username != expected_username:
            raise ConfigError(
                f"Instagram credential belongs to @{actual_username or 'unknown'}, expected @{expected_username}.",
                path="channels.instagram.expected_username",
            )
        if actual_ids and ig_user_id not in actual_ids:
            raise ConfigError(
                "Instagram credential user ID does not match ig_user_id.",
                path="channels.instagram.ig_user_id",
            )

    def _plan(self, post: PostData, urls: list[str], ig_user_id: str, host: str = INSTAGRAM_HOST) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        if post.kind == "carousel":
            for index, url in enumerate(urls, 1):
                params: dict[str, Any] = {
                    "is_carousel_item": "true",
                    "video_url" if media_type(url) == "video" else "image_url": url,
                }
                calls.append(self._planned("POST", f"{ig_user_id}/media", params, f"child_creation_id_{index}", host=host))
                if media_type(url) == "video":
                    calls.append(self._planned("GET", f"{{child_creation_id_{index}}}", {"fields": "status_code"}, host=host))
            parent: dict[str, Any] = {
                "media_type": "CAROUSEL",
                "children": ",".join(f"{{child_creation_id_{i}}}" for i in range(1, len(urls) + 1)),
                "caption": post.caption or "",
            }
            if post.collaborators:
                parent["collaborators"] = ",".join(post.collaborators)
            calls.append(self._planned("POST", f"{ig_user_id}/media", parent, "creation_id", host=host))
        else:
            url_key = "video_url" if media_type(urls[0]) == "video" else "image_url"
            params = {url_key: urls[0]}
            if post.kind == "story":
                params["media_type"] = "STORIES"
            else:
                params["caption"] = post.caption or ""
                if post.kind == "reel":
                    params["media_type"] = "REELS"
                if post.collaborators:
                    params["collaborators"] = ",".join(post.collaborators)
            calls.append(self._planned("POST", f"{ig_user_id}/media", params, "creation_id", host=host))
        calls.extend(
            [
                self._planned("GET", "{creation_id}", {"fields": "status_code"}, host=host),
                self._planned("POST", f"{ig_user_id}/media_publish", {"creation_id": "{creation_id}"}, "media_id", host=host),
                self._planned("GET", "{media_id}", {"fields": "permalink"}, host=host),
            ]
        )
        if post.first_comment:
            calls.append(self._planned("POST", "{media_id}/comments", {"message": post.first_comment}, host=host))
        return calls

    def publish(self, post: PostData, attempt: AttemptData, ctx: Ctx) -> ReceiptData | Pending:
        ig_user_id = ctx.credentials.get("ig_user_id", "").strip()
        if not ig_user_id:
            raise ConfigError("ig_user_id is required.", path="channels.instagram.ig_user_id")
        if not ctx.dry_run:
            self._verify_account(ctx, ig_user_id)
        urls = [ctx.media_url(item) for item in post.media]
        if ctx.dry_run:
            calls = self._plan(post, urls, ig_user_id, graph_host(ctx.credentials))
            ctx.transcript.extend(calls)
            return ReceiptData(
                external_id=f"dry-run:instagram:{post.external_ref}",
                permalink=None,
                raw={"dry_run": True, "api_calls": calls},
            )

        child_ids: list[str] = []
        video_child_ids: list[str] = []
        if post.kind == "carousel":
            for index, url in enumerate(urls, 1):
                params: dict[str, Any] = {
                    "is_carousel_item": "true",
                    "video_url" if media_type(url) == "video" else "image_url": url,
                }
                ctx.checkpoint({"network_started": True, "phase": f"child_{index}"})
                payload = self._call("POST", f"{ig_user_id}/media", params, ctx)
                child_id = _id(payload, "Instagram child container")
                child_ids.append(child_id)
                if media_type(url) == "video":
                    video_child_ids.append(child_id)
                ctx.checkpoint({"child_creation_ids": child_ids})
            handle = {
                "stage": "children" if video_child_ids else "create_parent",
                "child_ids": child_ids,
                "video_child_ids": video_child_ids,
                "post": _post_handle(post),
            }
            return Pending(handle)

        params = {"video_url" if media_type(urls[0]) == "video" else "image_url": urls[0]}
        if post.kind == "story":
            params["media_type"] = "STORIES"
        else:
            params["caption"] = post.caption or ""
            if post.kind == "reel":
                params["media_type"] = "REELS"
            if post.collaborators:
                params["collaborators"] = ",".join(post.collaborators)
        ctx.checkpoint({"network_started": True, "phase": "container"})
        payload = self._call("POST", f"{ig_user_id}/media", params, ctx)
        creation_id = _id(payload, "Instagram container")
        ctx.checkpoint({"creation_id": creation_id})
        return Pending({"stage": "container", "creation_id": creation_id, "post": _post_handle(post)})

    def poll(self, pending: Pending, ctx: Ctx) -> ReceiptData | Pending | Failed:
        handle = dict(pending.handle)
        ig_user_id = ctx.credentials["ig_user_id"]
        stage = handle["stage"]
        if stage == "children":
            for child_id in handle["video_child_ids"]:
                status = self._call("GET", child_id, {"fields": "status_code"}, ctx)
                state = str(status.get("status_code") or "").upper()
                if state == "ERROR":
                    return Failed(f"Instagram container {child_id} failed.")
                if state != "FINISHED":
                    return pending
            handle["stage"] = "create_parent"
            stage = "create_parent"
        if stage == "create_parent":
            post = handle["post"]
            params: dict[str, Any] = {
                "media_type": "CAROUSEL",
                "children": ",".join(handle["child_ids"]),
                "caption": post["caption"],
            }
            if post["collaborators"]:
                params["collaborators"] = ",".join(post["collaborators"])
            ctx.checkpoint({"network_started": True, "phase": "carousel_parent"})
            payload = self._call("POST", f"{ig_user_id}/media", params, ctx)
            creation_id = _id(payload, "Instagram carousel container")
            ctx.checkpoint({"creation_id": creation_id})
            handle.update(stage="container", creation_id=creation_id)
            return Pending(handle)
        if stage == "container":
            creation_id = handle["creation_id"]
            status = self._call("GET", creation_id, {"fields": "status_code"}, ctx)
            state = str(status.get("status_code") or "").upper()
            if state == "ERROR":
                return Failed(f"Instagram container {creation_id} failed.")
            if state != "FINISHED":
                return pending
            ctx.checkpoint({"network_started": True, "phase": "media_publish"})
            published = self._call("POST", f"{ig_user_id}/media_publish", {"creation_id": creation_id}, ctx)
            media_id = _id(published, "Instagram media")
            ctx.checkpoint({"media_id": media_id})
            permalink_payload = self._call("GET", media_id, {"fields": "permalink"}, ctx)
            first_comment_error = None
            comment = handle["post"].get("first_comment")
            if comment:
                try:
                    self._call("POST", f"{media_id}/comments", {"message": comment}, ctx)
                except ApiError as exc:
                    first_comment_error = exc.detail
            return ReceiptData(
                external_id=media_id,
                permalink=str(permalink_payload.get("permalink") or "") or None,
                raw={"creation_id": creation_id, "first_comment_error": first_comment_error},
            )
        return Failed("Instagram pending handle is invalid.")

    def lookup(self, attempt: AttemptData, ctx: Ctx):
        media_id = str(attempt.remote_ids.get("media_id") or "")
        if media_id:
            try:
                payload = self._call("GET", media_id, {"fields": "id,permalink"}, ctx)
            except ApiError as exc:
                if exc.not_found:
                    return NotFound()
                return Unknown(exc.detail)
            return Found(ReceiptData(media_id, str(payload.get("permalink") or "") or None, payload))
        if not attempt.remote_ids.get("network_started"):
            return NotFound()
        return Unknown("Instagram may have accepted a request before returning an identifier.")

    def edit(self, receipt: ReceiptData, caption: str, ctx: Ctx) -> ReceiptData:
        raise InvalidInput("instagram.edit is not supported.", path="capabilities.instagram.features.edit")

    def delete(self, receipt: ReceiptData, ctx: Ctx) -> None:
        raise InvalidInput("instagram.delete is not supported.", path="capabilities.instagram.features.delete")

    def insights(self, receipts: list[ReceiptData], ctx: Ctx) -> list[MetricData]:
        fetched_at = datetime.now(timezone.utc)
        result: list[MetricData] = []
        for receipt in receipts:
            payload = self._call("GET", f"{receipt.external_id}/insights", {"metric": ",".join(METRICS)}, ctx)
            for item in payload.get("data", []):
                if isinstance(item, dict) and item.get("name"):
                    value = item.get("values", item.get("value"))
                    result.append(
                        MetricData(
                            str(item["name"]),
                            value,
                            fetched_at,
                            item,
                            receipt.external_id,
                        )
                    )
        return result


def _id(payload: dict[str, Any], label: str) -> str:
    value = str(payload.get("id") or "").strip()
    if not value:
        raise ApiError(f"{label} response had no id.", definitive=False)
    return value


def _post_handle(post: PostData) -> dict[str, Any]:
    return {
        "caption": post.caption or "",
        "first_comment": post.first_comment,
        "collaborators": list(post.collaborators),
    }
