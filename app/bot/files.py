"""Приём файлов от пользователя: проверка типа/размера и сохранение в data/uploads/{order_id}/."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.types import Message

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 20 * 1024 * 1024  # лимит Bot API на скачивание
USER_DOC_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}


@dataclass
class IncomingFile:
    file_type: str
    file_id: str
    unique_id: str
    file_name: str | None
    mime_type: str | None
    size: int | None

    @property
    def ext(self) -> str:
        if self.file_type == "photo":
            return ".jpg"
        if self.file_name and "." in self.file_name:
            return "." + self.file_name.rsplit(".", 1)[1].lower()[:8]
        return mimetypes.guess_extension(self.mime_type or "") or ".bin"


def user_file(message: Message) -> IncomingFile | None:
    """Фото или документ (PDF/картинка) до 20 МБ, иначе None."""
    if message.photo:
        p = message.photo[-1]
        return IncomingFile("photo", p.file_id, p.file_unique_id, None, "image/jpeg", p.file_size)
    d = message.document
    if d is None:
        return None
    mime = (d.mime_type or "").lower()
    if mime not in USER_DOC_MIME or (d.file_size or 0) > MAX_DOWNLOAD:
        return None
    return IncomingFile("document", d.file_id, d.file_unique_id, d.file_name, mime, d.file_size)


async def download(bot: Bot, f: IncomingFile, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await bot.download(f.file_id, destination=dest)
        return dest
    except Exception:  # noqa: BLE001 — локальная копия не критична, file_id остаётся
        log.exception("не удалось скачать файл %s", f.file_id)
        return None


def file_fields(f: IncomingFile, local: Path | None) -> dict:
    return dict(file_type=f.file_type, tg_file_id=f.file_id, tg_file_unique_id=f.unique_id,
                file_name=f.file_name, mime_type=f.mime_type, size=f.size,
                local_path=str(local) if local else None)


# ---- «подтверждение» после пачки файлов: одно сообщение на альбом, а не на каждое фото
ACK_DELAY = 1.2
_acks: dict[tuple[int, str], asyncio.Task] = {}
_locks: dict[int, asyncio.Lock] = {}


def user_lock(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


def schedule_ack(user_id: int, kind: str, fn: Callable[[], Awaitable[None]]) -> None:
    key = (user_id, kind)
    old = _acks.get(key)
    if old is not None and not old.done():
        old.cancel()

    async def runner() -> None:
        try:
            await asyncio.sleep(ACK_DELAY)
            await fn()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("ack failed")
        finally:
            if _acks.get(key) is task:
                _acks.pop(key, None)

    task = asyncio.create_task(runner())
    _acks[key] = task


async def flush_acks() -> None:
    """Для тестов: дождаться всех отложенных подтверждений."""
    while _acks:
        await asyncio.gather(*list(_acks.values()), return_exceptions=True)


def cancel_ack(user_id: int, kind: str) -> None:
    task = _acks.pop((user_id, kind), None)
    if task is not None and not task.done():
        task.cancel()
