"""Мелочи интерфейса: убрать кнопки у старого сообщения, показать экран редактированием."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)


async def drop_kb(call: CallbackQuery) -> None:
    """Убирает кнопки у сообщения, на котором нажали, - чтобы не нажимали старые."""
    msg = call.message
    if not isinstance(msg, Message) or msg.reply_markup is None:
        return
    try:
        await msg.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as e:
        log.debug("drop_kb: %s", e)


async def show(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> Message | None:
    """Перерисовывает экран в том же сообщении; если нельзя (фото/документ/старое) - шлёт новое.
    Возвращает сообщение, в котором теперь экран."""
    msg = call.message
    if isinstance(msg, Message) and msg.text is not None:
        try:
            await msg.edit_text(text, reply_markup=kb)
            return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return msg
            log.debug("show: edit failed: %s", e)
    if call.message is None:
        return None
    return await call.message.answer(text, reply_markup=kb)
