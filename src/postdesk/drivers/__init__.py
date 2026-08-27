from __future__ import annotations

from .facebook import FacebookDriver
from .instagram import InstagramDriver


def default_drivers() -> dict[str, object]:
    return {"instagram": InstagramDriver(), "facebook": FacebookDriver()}
