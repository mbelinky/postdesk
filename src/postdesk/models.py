from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("tenant", "external_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100), index=True)
    external_ref: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(30), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    media_json: Mapped[str] = mapped_column(Text, default="[]")
    caption: Mapped[str | None] = mapped_column(Text)
    first_comment: Mapped[str | None] = mapped_column(Text)
    collaborators_json: Mapped[str] = mapped_column(Text, default="[]")
    link: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True, default="draft")
    claim_token: Mapped[str | None] = mapped_column(String(64), index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    notify_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    attempts: Mapped[list[PublishAttempt]] = relationship(back_populates="post")
    receipts: Mapped[list[Receipt]] = relationship(back_populates="post")


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    claim_token: Mapped[str] = mapped_column(String(64), unique=True)
    external_ref: Mapped[str] = mapped_column(String(200), index=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    remote_ids_json: Mapped[str] = mapped_column(Text, default="{}")
    pending_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    post: Mapped[Post] = relationship(back_populates="attempts")
    receipt: Mapped[Receipt | None] = relationship(back_populates="attempt", uselist=False)


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("tenant", "channel", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("publish_attempts.id"), unique=True)
    channel: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(200))
    permalink: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    post: Mapped[Post] = relationship(back_populates="receipts")
    attempt: Mapped[PublishAttempt] = relationship(back_populates="receipt")
    metrics: Mapped[list[Metric]] = relationship(back_populates="receipt")


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(100), index=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    value_json: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    receipt: Mapped[Receipt] = relationship(back_populates="metrics")
