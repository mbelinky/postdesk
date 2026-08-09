from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".m4v", ".mov", ".mp4", ".webm"}


def media_type(value: str) -> str | None:
    suffix = Path(urlparse(value).path).suffix.casefold()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def common_validation(post, capability) -> list[str]:
    problems: list[str] = []
    count = len(post.media)
    if count < capability.media_min or count > capability.media_max:
        problems.append(
            f"{post.channel}.{post.kind}.media requires "
            f"{capability.media_min}..{capability.media_max} values; got {count}."
        )
    caption_present = bool((post.caption or "").strip())
    if capability.caption == "required" and not caption_present:
        problems.append(f"{post.channel}.{post.kind}.caption is required.")
    if capability.caption == "forbidden" and caption_present:
        problems.append(f"{post.channel}.{post.kind}.caption is not supported.")
    link_present = bool((post.link or "").strip())
    if capability.link == "required" and not link_present:
        problems.append(f"{post.channel}.{post.kind}.link is required.")
    if capability.link == "forbidden" and link_present:
        problems.append(f"{post.channel}.{post.kind}.link is not supported.")
    for index, value in enumerate(post.media):
        kind = media_type(value)
        if kind not in capability.media_types:
            allowed = ", ".join(capability.media_types) or "no media"
            problems.append(
                f"{post.channel}.{post.kind}.media[{index}] must be {allowed}."
            )
    return problems

