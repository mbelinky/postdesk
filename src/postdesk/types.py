from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Protocol


@dataclass(frozen=True)
class KindCapability:
    media_min: int
    media_max: int
    media_types: tuple[str, ...]
    caption: Literal["required", "optional", "forbidden"]
    link: Literal["required", "optional", "forbidden"] = "forbidden"
    async_publish: bool = False
    max_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "media": {"min": self.media_min, "max": self.media_max},
            "media_types": list(self.media_types),
            "caption": self.caption,
            "link": self.link,
            "async_publish": self.async_publish,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class Capabilities:
    kinds: dict[str, KindCapability]
    features: dict[str, bool]
    metric_names: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kinds": {name: item.as_dict() for name, item in self.kinds.items()},
            "features": dict(self.features),
            "metric_names": list(self.metric_names),
        }


@dataclass
class PostData:
    id: int | None
    tenant: str
    channel: str
    kind: str
    media: list[str]
    caption: str | None = None
    first_comment: str | None = None
    collaborators: list[str] = field(default_factory=list)
    link: str | None = None
    external_ref: str | None = None


@dataclass
class AttemptData:
    id: int
    tenant: str
    external_ref: str
    remote_ids: dict[str, Any]


@dataclass
class ReceiptData:
    external_id: str
    permalink: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Pending:
    handle: dict[str, Any]


@dataclass(frozen=True)
class Failed:
    detail: str


@dataclass(frozen=True)
class Found:
    receipt: ReceiptData


@dataclass(frozen=True)
class NotFound:
    pass


@dataclass(frozen=True)
class Unknown:
    detail: str


LookupResult = Found | NotFound | Unknown
PublishResult = ReceiptData | Pending
PollResult = ReceiptData | Pending | Failed


@dataclass
class MetricData:
    name: str
    value: Any
    fetched_at: datetime
    raw: dict[str, Any]
    receipt_external_id: str


@dataclass
class Ctx:
    credentials: dict[str, str]
    media_url: Callable[[str], str]
    checkpoint: Callable[[dict[str, Any]], None]
    dry_run: bool
    timeout_seconds: float
    transcript: list[dict[str, Any]] = field(default_factory=list)


class Driver(Protocol):
    id: str

    def capabilities(self) -> Capabilities: ...
    def validate(self, post: PostData) -> list[str]: ...
    def publish(self, post: PostData, attempt: AttemptData, ctx: Ctx) -> PublishResult: ...
    def poll(self, pending: Pending, ctx: Ctx) -> PollResult: ...
    def lookup(self, attempt: AttemptData, ctx: Ctx) -> LookupResult: ...
    def edit(self, receipt: ReceiptData, caption: str, ctx: Ctx) -> ReceiptData: ...
    def delete(self, receipt: ReceiptData, ctx: Ctx) -> None: ...
    def insights(self, receipts: list[ReceiptData], ctx: Ctx) -> list[MetricData]: ...
