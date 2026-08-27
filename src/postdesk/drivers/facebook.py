from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from .common import common_validation

BASE_URL = "https://graph.facebook.com/v23.0"
VIDEO_MAX_BYTES = 1_073_741_824


class FacebookDriver:
    id = "facebook"

    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or UrlLibTransport()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            kinds={
                "text": KindCapability(0, 0, (), "required"),
                "link": KindCapability(0, 0, (), "optional", link="required"),
                "photo": KindCapability(1, 1, ("image",), "optional"),
                "album": KindCapability(2, 10, ("image",), "optional"),
                "video": KindCapability(1, 1, ("video",), "optional", async_publish=True, max_bytes=VIDEO_MAX_BYTES),
            },
            features={
                "first_comment": False,
                "collaborators": False,
                "edit": True,
                "delete": True,
                "native_scheduling": False,
            },
            metric_names=(),
        )

    def validate(self, post: PostData) -> list[str]:
        capability = self.capabilities().kinds.get(post.kind)
        if capability is None:
            return [f"facebook.kind {post.kind!r} is not supported."]
        problems = common_validation(post, capability)
        if post.first_comment:
            problems.append("facebook.first_comment is not supported.")
        if post.collaborators:
            problems.append("facebook.collaborators is not supported.")
        if post.kind == "video" and post.media:
            parsed = urlparse(post.media[0])
            if not parsed.scheme:
                path = Path(post.media[0]).expanduser()
                if path.is_file() and path.stat().st_size > VIDEO_MAX_BYTES:
                    problems.append(f"facebook.video.media exceeds {VIDEO_MAX_BYTES} bytes.")
        return problems

    def _call(self, method: str, path: str, parameters: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
        values = {**parameters, "access_token": ctx.credentials["token"]}
        return self.transport.request(method, f"{BASE_URL}/{path.lstrip('/')}", values, timeout=ctx.timeout_seconds)

    def _planned(self, method: str, path: str, parameters: dict[str, Any], result: str | None = None) -> dict[str, Any]:
        call = {
            "method": method,
            "url": f"{BASE_URL}/{path.lstrip('/')}",
            "parameters": {**parameters, "access_token": "<token>"},
        }
        if result:
            call["result"] = result
        return call

    def _plan(self, post: PostData, urls: list[str], page_id: str) -> list[dict[str, Any]]:
        message = post.caption or ""
        if post.kind in {"text", "link"}:
            params = {"message": message}
            if post.link:
                params["link"] = post.link
            return [self._planned("POST", f"{page_id}/feed", params, "post_id")]
        if post.kind == "photo":
            return [self._planned("POST", f"{page_id}/photos", {"url": urls[0], "message": message}, "post_id")]
        if post.kind == "album":
            calls = [
                self._planned("POST", f"{page_id}/photos", {"url": url, "published": "false"}, f"photo_id_{index}")
                for index, url in enumerate(urls, 1)
            ]
            params: dict[str, Any] = {"message": message}
            for index in range(1, len(urls) + 1):
                params[f"attached_media[{index - 1}]"] = json.dumps({"media_fbid": f"{{photo_id_{index}}}"})
            calls.append(self._planned("POST", f"{page_id}/feed", params, "post_id"))
            return calls
        return [
            self._planned("POST", f"{page_id}/videos", {"file_url": urls[0], "description": message}, "video_id"),
            self._planned("GET", "{video_id}", {"fields": "status"}),
        ]

    def publish(self, post: PostData, attempt: AttemptData, ctx: Ctx) -> ReceiptData | Pending:
        page_id = ctx.credentials.get("page_id", "").strip()
        if not page_id:
            raise ConfigError("page_id is required.", path="channels.facebook.page_id")
        urls = [ctx.media_url(item) for item in post.media]
        if ctx.dry_run:
            calls = self._plan(post, urls, page_id)
            ctx.transcript.extend(calls)
            return ReceiptData(
                external_id=f"dry-run:facebook:{post.external_ref}",
                raw={"dry_run": True, "api_calls": calls},
            )
        message = post.caption or ""
        if post.kind in {"text", "link"}:
            params = {"message": message}
            if post.link:
                params["link"] = post.link
            return self._publish_sync(f"{page_id}/feed", params, attempt, ctx)
        if post.kind == "photo":
            return self._publish_sync(f"{page_id}/photos", {"url": urls[0], "message": message}, attempt, ctx)
        if post.kind == "album":
            photo_ids: list[str] = []
            for index, url in enumerate(urls, 1):
                ctx.checkpoint({"network_started": True, "phase": f"album_photo_{index}"})
                payload = self._call("POST", f"{page_id}/photos", {"url": url, "published": "false"}, ctx)
                photo_ids.append(_id(payload, "Facebook photo"))
                ctx.checkpoint({"photo_ids": photo_ids})
            params: dict[str, Any] = {"message": message}
            for index, photo_id in enumerate(photo_ids):
                params[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo_id})
            return self._publish_sync(f"{page_id}/feed", params, attempt, ctx)
        ctx.checkpoint({"network_started": True, "phase": "video"})
        payload = self._call("POST", f"{page_id}/videos", {"file_url": urls[0], "description": message}, ctx)
        video_id = _id(payload, "Facebook video")
        ctx.checkpoint({"post_id": video_id})
        return Pending({"post_id": video_id})

    def _publish_sync(self, path: str, parameters: dict[str, Any], attempt: AttemptData, ctx: Ctx) -> ReceiptData:
        ctx.checkpoint({"network_started": True, "phase": "publish"})
        payload = self._call("POST", path, parameters, ctx)
        post_id = _id(payload, "Facebook post")
        ctx.checkpoint({"post_id": post_id})
        return ReceiptData(post_id, str(payload.get("permalink_url") or "") or None, payload)

    def poll(self, pending: Pending, ctx: Ctx) -> ReceiptData | Pending | Failed:
        post_id = str(pending.handle.get("post_id") or "")
        if not post_id:
            return Failed("Facebook pending handle is invalid.")
        payload = self._call("GET", post_id, {"fields": "status,permalink_url"}, ctx)
        status = payload.get("status", {})
        state = str(status.get("video_status") if isinstance(status, dict) else status).lower()
        if state in {"error", "failed"}:
            return Failed(f"Facebook video {post_id} failed.")
        if state not in {"ready", "published", "complete", "completed"}:
            return pending
        return ReceiptData(post_id, str(payload.get("permalink_url") or "") or None, payload)

    def lookup(self, attempt: AttemptData, ctx: Ctx):
        post_id = str(attempt.remote_ids.get("post_id") or "")
        if post_id:
            try:
                payload = self._call("GET", post_id, {"fields": "id,permalink_url"}, ctx)
            except ApiError as exc:
                if exc.not_found:
                    return NotFound()
                return Unknown(exc.detail)
            return Found(ReceiptData(post_id, str(payload.get("permalink_url") or "") or None, payload))
        if not attempt.remote_ids.get("network_started"):
            return NotFound()
        return Unknown("Facebook may have accepted a request before returning an identifier.")

    def edit(self, receipt: ReceiptData, caption: str, ctx: Ctx) -> ReceiptData:
        payload = self._call("POST", receipt.external_id, {"message": caption}, ctx)
        return ReceiptData(receipt.external_id, receipt.permalink, {**receipt.raw, "edit": payload})

    def delete(self, receipt: ReceiptData, ctx: Ctx) -> None:
        self._call("DELETE", receipt.external_id, {}, ctx)

    def insights(self, receipts: list[ReceiptData], ctx: Ctx) -> list[MetricData]:
        fetched_at = datetime.now(timezone.utc)
        result: list[MetricData] = []
        for receipt in receipts:
            payload = self._call("GET", f"{receipt.external_id}/insights", {}, ctx)
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
    value = str(payload.get("id") or payload.get("post_id") or "").strip()
    if not value:
        raise ApiError(f"{label} response had no id.", definitive=False)
    return value
