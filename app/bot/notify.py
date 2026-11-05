from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.db.models import AdminMessage, OrderFile
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)
MarkupFor = Callable[[int], InlineKeyboardMarkup | None]  # уровень админа → кнопки


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        log.info("пользователь %s заблокировал бота", chat_id)
    except TelegramBadRequest as e:
        log.warning("не отправилось %s: %s", chat_id, e)
    except Exception:  # noqa: BLE001
        log.exception("ошибка отправки %s", chat_id)
    return None


def staff_targets(config: Config, min_level: int = 1, only: set[int] | None = None) -> list[tuple[int, int]]:
    """[(id, уровень)] админов с уровнем ≥ min_level; владельцы — первыми."""
    staff = config.staff
    ids = sorted(staff, key=lambda uid: (uid not in config.admin_ids, uid))
    return [(uid, staff[uid]) for uid in ids if staff[uid] >= min_level and (only is None or uid in only)]


async def notify_admins(bot: Bot, config: Config, text: str, *, min_level: int = 1,
                        markup_for: MarkupFor | None = None, exclude: int | None = None,
                        **kwargs) -> list[tuple[int, Message]]:
    """Шлёт всем админам (уровень ≥ min_level). markup_for(level) — кнопки по уровню доступа.
    Возвращает [(admin_id, сообщение)] — чтобы запомнить заголовки (ответ свайпом, пометки «взял»)."""
    sent: list[tuple[int, Message]] = []
    for admin_id, level in staff_targets(config, min_level):
        if admin_id == exclude:
            continue
        kw = dict(kwargs)
        if markup_for is not None:
            kw["reply_markup"] = markup_for(level)
        msg = await safe_send(bot, admin_id, text, **kw)
        if msg is not None:
            sent.append((admin_id, msg))
    return sent


def remember(session: AsyncSession, admin_id: int, message_id: int, kind: str, ref_id: int,
             user_id: int | None = None, header_text: str | None = None) -> None:
    """Запоминает сообщение у админа: ответ на него свайпом уйдёт клиенту, заголовок можно пометить. Без commit."""
    session.add(AdminMessage(admin_id=admin_id, message_id=message_id, kind=kind, ref_id=ref_id, user_id=user_id,
                             is_header=header_text is not None, text=header_text, created_at=now_utc()))


def remember_all(session: AsyncSession, sent: list[tuple[int, Message]], kind: str, ref_id: int,
                 user_id: int | None, text: str | None) -> None:
    for admin_id, msg in sent:
        remember(session, admin_id, msg.message_id, kind, ref_id, user_id, text)


async def lookup(session: AsyncSession, admin_id: int, message_id: int) -> AdminMessage | None:
    return await session.scalar(select(AdminMessage).where(AdminMessage.admin_id == admin_id,
                                                           AdminMessage.message_id == message_id).limit(1))


async def mark_headers(bot: Bot, session: AsyncSession, config: Config, kind: str, ref_id: int, marker: str,
                       markup_for: MarkupFor | None = None) -> int:
    """Дописывает к уведомлениям у всех админов строку «✅ Ответил(а): Аня» — чтобы не делать работу дважды.
    markup_for(level) — новые кнопки (None — кнопки убрать)."""
    rows = (await session.execute(select(AdminMessage).where(AdminMessage.kind == kind, AdminMessage.ref_id == ref_id,
                                                             AdminMessage.is_header.is_(True))
                                  .order_by(AdminMessage.id))).scalars().all()
    done = 0
    for row in rows:
        base = (row.text or "").split("\n\n—\n")[0]
        text = f"{base}\n\n—\n{marker}"
        markup = markup_for(config.admin_level(row.admin_id)) if markup_for else None
        try:
            await bot.edit_message_text(text[:4096], chat_id=row.admin_id, message_id=row.message_id,
                                        reply_markup=markup)
            done += 1
        except TelegramBadRequest as e:
            if "no text" in str(e).lower():  # уведомление с фото/файлом — правим подпись
                try:
                    await bot.edit_message_caption(chat_id=row.admin_id, message_id=row.message_id,
                                                   caption=text[:1024], reply_markup=markup)
                    done += 1
                    continue
                except TelegramBadRequest as e2:
                    e = e2
            log.debug("mark_headers %s/%s: %s", row.admin_id, row.message_id, e)
        except Exception:  # noqa: BLE001
            log.warning("mark_headers: не удалось обновить уведомление у %s", row.admin_id)
    return done


def _input(f: OrderFile):  # noqa: ANN202
    return FSInputFile(f.local_path, filename=f.file_name) if f.local_path and Path(f.local_path).exists() else None


async def send_files(bot: Bot, chat_id: int, files: list[OrderFile], caption: str | None = None) -> None:
    """Фото — альбомами по 10, документы — отдельно. Если file_id не сработал — шлём локальную копию."""
    photos = [f for f in files if f.file_type == "photo"]
    docs = [f for f in files if f.file_type != "photo"]
    first = True
    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        cap = caption if first else None
        try:
            if len(chunk) == 1:
                await bot.send_photo(chat_id, chunk[0].tg_file_id, caption=cap)
            else:
                await bot.send_media_group(chat_id, [InputMediaPhoto(media=f.tg_file_id, caption=cap if j == 0 else None)
                                                     for j, f in enumerate(chunk)])
        except TelegramBadRequest:
            for f in chunk:
                local = _input(f)
                if local:
                    await bot.send_photo(chat_id, local, caption=cap)
                    cap = None
        first = False
    for i in range(0, len(docs), 10):
        chunk = docs[i:i + 10]
        cap = caption if first else None
        try:
            if len(chunk) == 1:
                await bot.send_document(chat_id, chunk[0].tg_file_id, caption=cap)
            else:
                await bot.send_media_group(chat_id, [InputMediaDocument(media=f.tg_file_id,
                                                                        caption=cap if j == 0 else None)
                                                     for j, f in enumerate(chunk)])
        except TelegramBadRequest:
            for f in chunk:
                local = _input(f)
                if local:
                    await bot.send_document(chat_id, local, caption=cap)
                    cap = None
        first = False
