from __future__ import annotations

from .facebook import FacebookDriver
from .instagram import InstagramDriver
from .youtube import YouTubeDriver

CHANNELS = ("instagram", "facebook", "youtube")


def default_drivers() -> dict[str, object]:
    return {"instagram": InstagramDriver(), "facebook": FacebookDriver(), "youtube": YouTubeDriver()}
