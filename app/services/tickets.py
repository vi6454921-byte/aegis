"""Обращения в поддержку: вопросы, оплата, вопросы по заказу, обжалование бана.

Переписка с клиентом идёт через бота: клиент пишет - админам приходит уведомление с кнопками
(«✉️ Ответить», «✅ Закрыть»), админ отвечает кнопкой или просто свайпом-ответом на уведомление.
Сообщения клиента в течение THREAD_WINDOW склеиваются в одно обращение.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketMessage

THREAD_WINDOW = timedelta(days=3)
KINDS = {"support": "💬 Вопрос в поддержку", "pay": "💳 Вопрос по оплате", "appeal": "📨 Обжалование бана",
         "order": "💬 Вопрос по заказу"}
STATUS = {"open": "🔴 ждёт ответа", "answered": "✅ отвечено", "closed": "☑️ закрыто"}


def title(ticket: Ticket) -> str:
    base = KINDS.get(ticket.kind, KINDS["support"])
    return f"{base} #{ticket.order_id}" if ticket.kind == "order" and ticket.order_id else base


def preview_of(text: str | None, content_type: str) -> str:
    t = (text or "").strip()
    if content_type != "text":
        label = {"photo": "📷 фото", "document": "📎 файл", "video": "🎬 видео", "voice": "🎤 голосовое",
                 "audio": "🎵 аудио", "sticker": "стикер", "video_note": "🎥 кружок", "animation": "GIF",
                 "album": "🖼 альбом"}.get(content_type, content_type)
        t = f"[{label}] {t}".strip()
    return t[:600]


async def open_or_append(session: AsyncSession, user_id: int, kind: str, now: datetime,
                         order_id: int | None = None) -> tuple[Ticket, bool]:
    """Находит незакрытое свежее обращение того же типа или создаёт новое. (обращение, создано ли).

    v2.3: переписка по заказу - одна на заказ, без окна и независимо от «закрыто»: все сообщения админов
    и клиента по заказу №N складываются в одну ветку (её видно в карточке заказа)."""
    if kind == "order" and order_id:
        q = select(Ticket).where(Ticket.user_id == user_id, Ticket.kind == "order", Ticket.order_id == order_id)
    else:
        q = select(Ticket).where(Ticket.user_id == user_id, Ticket.kind == kind, Ticket.status != "closed",
                                 Ticket.updated_at >= now - THREAD_WINDOW)
    ticket = await session.scalar(q.order_by(Ticket.id.desc()).limit(1))
    if ticket is not None:
        return ticket, False
    ticket = Ticket(user_id=user_id, kind=kind, status="open", order_id=order_id, created_at=now, updated_at=now)
    session.add(ticket)
    await session.flush()
    return ticket, True


async def get(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id, populate_existing=True)


def add_in(session: AsyncSession, ticket: Ticket, content_type: str, text: str | None, user_msg_id: int | None,
           now: datetime) -> TicketMessage:
    """Сообщение клиента. Без commit."""
    ticket.status = "open"
    ticket.preview = preview_of(text, content_type)
    ticket.last_in_at = now
    ticket.updated_at = now
    msg = TicketMessage(ticket_id=ticket.id, direction="in", content_type=content_type, text=text,
                        user_msg_id=user_msg_id, created_at=now)
    session.add(msg)
    return msg


def add_out(session: AsyncSession, ticket: Ticket, admin_id: int, content_type: str, text: str | None,
            user_msg_id: int | None, now: datetime) -> TicketMessage:
    """Ответ админа (user_msg_id - id доставленного сообщения в чате клиента). Без commit."""
    ticket.status = "answered"
    ticket.answered_by = admin_id
    ticket.answered_at = now
    ticket.updated_at = now
    msg = TicketMessage(ticket_id=ticket.id, direction="out", admin_id=admin_id, content_type=content_type,
                        text=text, user_msg_id=user_msg_id, created_at=now)
    session.add(msg)
    return msg


async def close(session: AsyncSession, ticket: Ticket, admin_id: int, now: datetime) -> None:
    ticket.status = "closed"
    ticket.closed_by = admin_id
    ticket.closed_at = now
    ticket.updated_at = now
    await session.commit()


async def by_user_reply(session: AsyncSession, user_id: int, replied_msg_id: int) -> Ticket | None:
    """Клиент ответил (свайпом) на сообщение поддержки - находим обращение."""
    return await session.scalar(select(Ticket).join(TicketMessage, TicketMessage.ticket_id == Ticket.id)
                                .where(Ticket.user_id == user_id, TicketMessage.direction == "out",
                                       TicketMessage.user_msg_id == replied_msg_id).limit(1))


async def messages(session: AsyncSession, ticket_id: int, limit: int = 12) -> list[TicketMessage]:
    rows = (await session.execute(select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
                                  .order_by(TicketMessage.id.desc()).limit(limit))).scalars().all()
    return list(reversed(rows))


async def order_thread(session: AsyncSession, order_id: int, limit: int = 50) -> tuple[list[TicketMessage], int]:
    """Вся переписка по заказу (все его обращения): последние limit сообщений по порядку и сколько их всего.
    Заголовки альбомов (content_type=album) не считаем - сами фото альбома записаны отдельно."""
    base = (select(TicketMessage).join(Ticket, Ticket.id == TicketMessage.ticket_id)
            .where(Ticket.order_id == order_id, TicketMessage.content_type != "album"))
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = (await session.execute(base.order_by(TicketMessage.id.desc()).limit(limit))).scalars().all()
    return list(reversed(rows)), total


async def open_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(Ticket.id)).where(Ticket.status == "open")) or 0)


async def inbox(session: AsyncSession, page: int, per_page: int, only_open: bool = True) -> tuple[list[Ticket], int]:
    q = select(Ticket)
    if only_open:
        q = q.where(Ticket.status == "open")
    else:
        q = q.where(Ticket.status != "closed")
    total = int(await session.scalar(select(func.count()).select_from(q.subquery())) or 0)
    order = (Ticket.last_in_at.asc(), Ticket.id.asc()) if only_open else (Ticket.updated_at.desc(),)
    rows = (await session.execute(q.order_by(*order).offset(page * per_page).limit(per_page))).scalars().all()
    return list(rows), total


async def last_appeal(session: AsyncSession, user_id: int) -> Ticket | None:
    return await session.scalar(select(Ticket).where(Ticket.user_id == user_id, Ticket.kind == "appeal")
                                .order_by(Ticket.id.desc()).limit(1))
