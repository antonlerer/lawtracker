from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # в БД храним UTC без tzinfo


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(40), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verify_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0)
    reset_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reset_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sessions = relationship("AuthSession", cascade="all, delete-orphan", back_populates="user")
    member = relationship("Member", back_populates="user", uselist=False, foreign_keys="Member.user_id")


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    user = relationship("User", back_populates="sessions")


class LoginAttempt(Base):
    """Для ограничения перебора паролей и злоупотреблений формами (ключ — email или IP)."""
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(300), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="login")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan: Mapped[str] = mapped_column(String(20), default="free")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)          # значения полей CompanyProfile
    steps_saved: Mapped[dict] = mapped_column(JSON, default=dict)      # {step_id: true}
    steps_skipped: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    members = relationship("Member", cascade="all, delete-orphan", back_populates="company")


class Member(Base):
    """Участник компании или приглашение (status='pending', user_id пока пуст)."""
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_member_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    role: Mapped[str] = mapped_column(String(10), default="member")
    status: Mapped[str] = mapped_column(String(10), default="active")   # active | pending
    invite_code: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)
    invite_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    company = relationship("Company", back_populates="members")
    user = relationship("User", back_populates="member", foreign_keys=[user_id])


class Change(Base):
    """Изменение законодательства с атрибутами для персонализации."""
    __tablename__ = "changes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)  # eoNumber портала
    topic: Mapped[str] = mapped_column(String(10), index=True)
    impact: Mapped[str] = mapped_column(String(5), index=True)
    published_at: Mapped[date] = mapped_column(Date, index=True)
    effective_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(10), default="published", index=True)   # published | hidden
    agency: Mapped[str] = mapped_column(String(200), default="")
    doc: Mapped[str] = mapped_column(String(300), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(5), default="low")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    what: Mapped[str] = mapped_column(Text, default="")
    who: Mapped[str] = mapped_column(Text, default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    requires: Mapped[list] = mapped_column(JSON, default=list)      # правила, которые обязаны выполняться
    reasons: Mapped[list] = mapped_column(JSON, default=list)       # [{"when": rule, "text": str}]
    regions: Mapped[list] = mapped_column(JSON, default=list)       # пусто = федеральное
    actions: Mapped[list] = mapped_column(JSON, default=list)       # [{"title", "due", "responsible"}]
    related: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChangeState(Base):
    """Личные отметки пользователя: прочитано / сохранено."""
    __tablename__ = "change_states"
    __table_args__ = (UniqueConstraint("user_id", "change_id", name="uq_state"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("changes.id", ondelete="CASCADE"), index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    saved: Mapped[bool] = mapped_column(Boolean, default=False)


class ActionState(Base):
    """Выполнение рекомендованных действий — общее для компании."""
    __tablename__ = "action_states"
    __table_args__ = (UniqueConstraint("company_id", "change_id", "idx", name="uq_action"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    change_id: Mapped[str] = mapped_column(ForeignKey("changes.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10), default="todo")
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NotificationPrefs(Base):
    __tablename__ = "notification_prefs"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class TelegramLink(Base):
    __tablename__ = "telegram_links"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default="none")    # none | pending | connected | error | disabled
    code: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    code_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)
    username: Mapped[str] = mapped_column(String(80), default="")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class NotificationLog(Base):
    """Что и когда отправлено: защита от повторов и «последняя отправка» для расписания."""
    __tablename__ = "notification_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(10), default="digest")   # digest | urgent | test
    change_ids: Mapped[list] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


Index("ix_notiflog_user_channel", NotificationLog.user_id, NotificationLog.channel, NotificationLog.sent_at)


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    topic: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PlanRequest(Base):
    __tablename__ = "plan_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    plan: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(5), default="m")
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Waitlist(Base):
    __tablename__ = "waitlist"
    __table_args__ = (UniqueConstraint("user_id", "feature", name="uq_waitlist"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    feature: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
