"""Схема БД. Меняется только миграциями Alembic (migrations/versions)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.utils.timeutil import now_utc


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # Telegram id
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(16), default="direct")  # direct | ref | src
    source_tag: Mapped[str | None] = mapped_column(String(64), index=True)
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    ref_code: Mapped[str] = mapped_column(String(32), unique=True)
    balance_rub: Mapped[int] = mapped_column(BigInteger, default=0)  # кэш, копейки
    balance_bonus: Mapped[int] = mapped_column(BigInteger, default=0)  # кэш, копейки
    is_priority: Mapped[bool] = mapped_column(Boolean, default=False)
    priority_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    free_month_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    free_month_tasks_left: Mapped[int] = mapped_column(Integer, default=0)
    free_month_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    remind_tomorrow: Mapped[bool] = mapped_column(Boolean, default=False)
    phone: Mapped[str | None] = mapped_column(String(32))  # только если пользователь сам поделился контактом
    tutorial_done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # бан и обжалование
    ban_reason: Mapped[str | None] = mapped_column(Text)
    banned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    appeal_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    appeals_count: Mapped[int] = mapped_column(Integer, default=0)
    # подписка на канал (бонус - один раз на аккаунт)
    channel_status: Mapped[str | None] = mapped_column(String(16))  # member | left
    channel_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    channel_bonus_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    channel_bonus_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    channel_bonus_revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # безлимит (v2.3): навсегда или до даты; источник - промокод (unlimited_code_id) или админ (unlimited_by)
    unlimited_forever: Mapped[bool] = mapped_column(Boolean, default=False)
    unlimited_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    unlimited_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    unlimited_code_id: Mapped[int | None] = mapped_column(Integer)
    unlimited_by: Mapped[int | None] = mapped_column(BigInteger)
    unlimited_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def unlimited_active(self, now: datetime) -> bool:
        if self.unlimited_forever:
            return True
        return self.unlimited_until is not None and self.unlimited_until > now


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    subject: Mapped[str | None] = mapped_column(String(16))  # nach | calc | explain | any | design (v2.4)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    wizard_step: Mapped[str | None] = mapped_column(String(24))
    task_count: Mapped[int | None] = mapped_column(Integer)
    variant: Mapped[str | None] = mapped_column(String(64))
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    lecture_status: Mapped[str] = mapped_column(String(16), default="none")  # none|pending|approved|rejected
    practice_status: Mapped[str] = mapped_column(String(16), default="none")
    price_per_task: Mapped[int | None] = mapped_column(BigInteger)
    urgency_pct: Mapped[int | None] = mapped_column(Integer)
    lecture_discount_pct: Mapped[int] = mapped_column(Integer, default=0)
    practice_discount_pct: Mapped[int] = mapped_column(Integer, default=0)
    discount_pct: Mapped[int] = mapped_column(Integer, default=0)
    free_tasks: Mapped[int] = mapped_column(Integer, default=0)
    price_auto: Mapped[int | None] = mapped_column(BigInteger)
    price_override: Mapped[int | None] = mapped_column(BigInteger)
    price_final: Mapped[int | None] = mapped_column(BigInteger)
    paid_bonus: Mapped[int] = mapped_column(BigInteger, default=0)
    paid_balance: Mapped[int] = mapped_column(BigInteger, default=0)
    paid_invoice: Mapped[int] = mapped_column(BigInteger, default=0)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    had_dispute: Mapped[bool] = mapped_column(Boolean, default=False)
    redo_count: Mapped[int] = mapped_column(Integer, default=0)
    solution_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    solution_entry: Mapped[str | None] = mapped_column(String(255))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    slot_date: Mapped[date | None] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, onupdate=now_utc)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    invoiced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    assignee_id: Mapped[int | None] = mapped_column(BigInteger)  # кто из админов взял в работу
    chat_replies: Mapped[int] = mapped_column(Integer, default=0)  # сообщений клиенту по заказу от админов
    pay_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deadline_alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False)  # бесплатно по безлимиту (промокод)
    draft_warned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # предупредили, что черновик удалится
    # v2.4: «📎 Любая задача»: когда и кто из админов назвал цену (статус quoted)
    quoted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    quoted_by: Mapped[int | None] = mapped_column(BigInteger)
    # v2.4: «🎨 Файлы и дизайн»: тип (pres|pdf|img|banner|qr), оформление презентации (custom|ssau),
    # формат (16:9|4:3) и язык (ru|en) шаблона университета. Количество - task_count.
    design_kind: Mapped[str | None] = mapped_column(String(16))
    design_style: Mapped[str | None] = mapped_column(String(16))
    design_format: Mapped[str | None] = mapped_column(String(8))
    design_lang: Mapped[str | None] = mapped_column(String(8))

    @property
    def price(self) -> int | None:
        """Актуальная цена: выставленная в счёте, иначе ручная, иначе расчётная."""
        if self.price_final is not None:
            return self.price_final
        if self.price_override is not None:
            return self.price_override
        return self.price_auto

    @property
    def rub_paid(self) -> int:
        return (self.paid_balance or 0) + (self.paid_invoice or 0)


class OrderFile(Base):
    __tablename__ = "order_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # condition | lecture | practice | dispute | solution
    file_type: Mapped[str] = mapped_column(String(16))  # photo | document
    tg_file_id: Mapped[str] = mapped_column(String(255))
    tg_file_unique_id: Mapped[str | None] = mapped_column(String(128))
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(Integer)
    local_path: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # order | topup
    provider: Mapped[str] = mapped_column(String(32))  # test | stars | internal | (будущий провайдер)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|paid|refunded|cancelled
    amount_rub: Mapped[int] = mapped_column(BigInteger, default=0)  # сколько денег приходит по счёту, копейки
    amount_stars: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    plan_bonus: Mapped[int] = mapped_column(BigInteger, default=0)
    plan_balance: Mapped[int] = mapped_column(BigInteger, default=0)
    topup_bonus: Mapped[int] = mapped_column(BigInteger, default=0)
    payload: Mapped[str] = mapped_column(String(128), unique=True)
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(255))
    # задел под платёжный сервис для самозанятых (webhook + чеки)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), index=True)
    receipt_url: Mapped[str | None] = mapped_column(String(1024))
    receipt_status: Mapped[str | None] = mapped_column(String(32))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    refund_method: Mapped[str | None] = mapped_column(String(16))  # balance | stars
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # ручная оплата переводом: клиент присылает чек, админ подтверждает
    receipt_file_id: Mapped[str | None] = mapped_column(String(255))
    receipt_file_type: Mapped[str | None] = mapped_column(String(16))
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    admin_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class BonusLot(Base):
    __tablename__ = "bonus_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    remaining: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(32))  # referral | topup_bonus | refund | admin
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    expired: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    referral_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("referrals.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class Transaction(Base):
    """Журнал денег: только добавление. Каждая бонусная строка ссылается на свою партию."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    account: Mapped[str] = mapped_column(String(8))  # rub | bonus
    amount: Mapped[int] = mapped_column(BigInteger)  # со знаком, копейки
    kind: Mapped[str] = mapped_column(String(32))
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"), index=True)
    payment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("payments.id"), index=True)
    bonus_lot_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bonus_lots.id"), index=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, index=True)


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    rewarded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reward_order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"))
    reward_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    reward_is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    rewarded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_count: Mapped[int] = mapped_column(Integer, default=0)
    # бонус пригласившему за подписку друга на канал: none → pending → paid | cancelled
    sub_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    sub_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sub_rewarded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sub_reward_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    sub_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, onupdate=now_utc)


class GiftLink(Base):
    __tablename__ = "gift_links"
    __table_args__ = (UniqueConstraint("user_id", "gift", name="uq_gift_links_user_gift"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    gift: Mapped[str] = mapped_column(String(32))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class GiftOpen(Base):
    __tablename__ = "gift_opens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    gift: Mapped[str] = mapped_column(String(32), index=True)
    via: Mapped[str] = mapped_column(String(16))  # webapp | document
    user_agent: Mapped[str | None] = mapped_column(String(512))
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, index=True)


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    segment: Mapped[str] = mapped_column(String(32))
    from_chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|sending|done|cancelled
    total: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AdminLog(Base):
    __tablename__ = "admin_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text)  # JSON
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, index=True)


class Admin(Base):
    """Админы из бота: 1 - оператор (заказы, оплаты, поддержка), 2 - старший (всё). Владельцы - из ADMIN_IDS."""

    __tablename__ = "admins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    level: Mapped[int] = mapped_column(Integer, default=1)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    # закреплённый админ 2 уровня (SENIOR_ADMIN_USERNAMES / SENIOR_ADMIN_IDS): другие админы его не снимают
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)


class PinnedAdmin(Base):
    """Привязка закреплённого админа: ник из .env → Telegram id (один раз), либо id из SENIOR_ADMIN_IDS.

    После привязки права - только по id: смена ника их не снимает, новый владелец ника ничего не получает.
    revoked_at - владелец снял этого админа в боте: по нику права больше не выдаются (пока ник в .env)."""

    __tablename__ = "pinned_admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True)  # в нижнем регистре; None - по id
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source: Mapped[str] = mapped_column(String(16), default="username")  # username | id
    bound_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger)


class AdminInvite(Base):
    __tablename__ = "admin_invites"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_by: Mapped[int | None] = mapped_column(BigInteger)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Ticket(Base):
    """Обращение в поддержку: вопрос, оплата, обжалование бана, вопрос по заказу."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="support")  # support | pay | appeal | order
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open | answered | closed
    order_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("orders.id"))
    preview: Mapped[str | None] = mapped_column(String(600))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    last_in_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    answered_by: Mapped[int | None] = mapped_column(BigInteger)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_by: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickets.id"), index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in - от клиента, out - от админа
    admin_id: Mapped[int | None] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(16), default="text")
    text: Mapped[str | None] = mapped_column(Text)
    user_msg_id: Mapped[int | None] = mapped_column(Integer)  # id сообщения в чате клиента
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class AdminMessage(Base):
    """Что бот прислал админам: чтобы отвечать клиенту реплаем и отмечать «кто взял / кто ответил»."""

    __tablename__ = "admin_messages"
    __table_args__ = (UniqueConstraint("admin_id", "message_id", name="uq_admin_messages_admin_msg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # ticket | order | payment | user
    ref_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    is_header: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class ProfileClaim(Base):
    """Задание «Ссылка в профиле»: скрин профиля с реф-ссылкой в «О себе» → проверка админом → рубли.

    Награда - один раз на аккаунт: approved/revoked закрывают задание, после rejected можно прислать снова."""

    __tablename__ = "profile_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|rejected|revoked
    link: Mapped[str | None] = mapped_column(String(255))
    file_id: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[str | None] = mapped_column(String(16))  # photo | document
    bio_check: Mapped[str | None] = mapped_column(String(16))  # found | bot_only | missing | unknown
    reward: Mapped[int] = mapped_column(BigInteger, default=0)  # сколько начислено, копейки
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    recheck_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    rechecked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class PromoPost(Base):
    """Задание «Пост о нас»: пост в Telegram или ВКонтакте. Одна ссылка засчитывается один раз во всей системе."""

    __tablename__ = "promo_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(8))  # tg | vk
    post_url: Mapped[str] = mapped_column(String(512), unique=True)  # нормализованная ссылка
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|accepted|rejected|deleted
    base_reward_paid: Mapped[int] = mapped_column(BigInteger, default=0)
    verified_views: Mapped[int] = mapped_column(BigInteger, default=0)
    views_reward_paid: Mapped[int] = mapped_column(BigInteger, default=0)
    adjust_total: Mapped[int] = mapped_column(BigInteger, default=0)  # ручные корректировки (2 уровень)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deleted_by: Mapped[int | None] = mapped_column(BigInteger)
    last_report_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    last_update_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)

    @property
    def credited(self) -> int:
        return (self.base_reward_paid or 0) + (self.views_reward_paid or 0) + (self.adjust_total or 0)


class PromoPostReport(Base):
    """Отчёт по посту: first - первый скрин поста (награда за пост), views - скрин просмотров (накопительно)."""

    __tablename__ = "promo_post_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("promo_posts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # first | views
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|rejected
    file_id: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[str | None] = mapped_column(String(16))
    claimed_views: Mapped[int | None] = mapped_column(BigInteger)
    verified_views: Mapped[int | None] = mapped_column(BigInteger)
    credited: Mapped[int] = mapped_column(BigInteger, default=0)
    reviewer_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)


class PromoCode(Base):
    """Одноразовый промокод на безлимит: forever - навсегда, days - на `days` дней с момента активации."""

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # AEGIS-XXXX-XXXX
    kind: Mapped[str] = mapped_column(String(8))  # forever | days
    days: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    created_by: Mapped[int | None] = mapped_column(BigInteger)  # None - из миграции 0005
    used_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger)


class PromoAttempt(Base):
    """Попытка ввода промокода - для защиты от перебора."""

    __tablename__ = "promo_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    code: Mapped[str | None] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, index=True)


class StrayMessage(Base):
    """Фото, файл или текст вне оформления заказа и поддержки: ждём ответа «заказ или поддержка?»."""

    __tablename__ = "stray_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(Integer)
    media_group_id: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(String(255))
    file_unique_id: Mapped[str | None] = mapped_column(String(128))
    file_type: Mapped[str | None] = mapped_column(String(16))  # photo | document
    file_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(Integer)
    prompt_message_id: Mapped[int | None] = mapped_column(Integer)  # наш вопрос «Это задание?»
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|order|support|auto
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    order_id: Mapped[int | None] = mapped_column(Integer)
    ticket_id: Mapped[int | None] = mapped_column(Integer)


class AdminChat(Base):
    """Режим чата админа с клиентом: всё, что админ пишет боту, уходит клиенту, пока не нажмёт «Завершить»."""

    __tablename__ = "admin_chats"

    admin_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger)
    order_id: Mapped[int | None] = mapped_column(Integer)
    ticket_id: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc)
    last_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent: Mapped[int] = mapped_column(Integer, default=0)


class FSMRecord(Base):
    """Состояния диалогов aiogram - в БД, чтобы переживать рестарт."""

    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(255))
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now_utc, onupdate=now_utc)


Index("ix_orders_status_deadline", Order.status, Order.deadline_at)
Index("ix_admin_messages_kind_ref", AdminMessage.kind, AdminMessage.ref_id)
Index("ix_ticket_messages_user_msg", TicketMessage.user_msg_id)
