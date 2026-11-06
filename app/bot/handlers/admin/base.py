"""Общее для админки: роутеры с проверкой уровня доступа, кнопки «в админку» и «отмена», экраны-вопросы."""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.callbacks import AdminCb
from app.bot.filters import IsAdmin
from app.bot.keyboards import cb
from app.bot.ui import show

log = logging.getLogger(__name__)
L2 = IsAdmin(2)  # для отдельных хендлеров: только старшие админы


def admin_router(name: str, level: int = 1) -> Router:
    """Фильтр IsAdmin висит на message и callback_query роутера - значит, на каждом его хендлере."""
    router = Router(name=name)
    router.message.filter(IsAdmin(level))
    router.callback_query.filter(IsAdmin(level))
    return router


def menu_btn() -> InlineKeyboardButton:
    return cb("🛡 В админку", AdminCb(s="m"))


def cancel_btn(back: AdminCb | None = None, text: str = "✖️ Отмена") -> InlineKeyboardButton:
    """Отмена ввода. back - куда вернуться (экран перерисуется на месте), иначе просто убрать вопрос."""
    if back is None:
        return cb(text, AdminCb(s="x"))
    return cb(text, AdminCb(s="x", a=back.s, id=back.id, v=back.a + (f"|{back.v}" if back.v else ""), p=back.p))


def back_of(x: AdminCb) -> AdminCb | None:
    """Обратное к cancel_btn: куда вернуться после отмены."""
    if not x.a:
        return None
    a, _, v = x.v.partition("|")
    return AdminCb(s=x.a, a=a, id=x.id, p=x.p, v=v)


async def ask(call: CallbackQuery, state: FSMContext, st: State, text: str, kb: InlineKeyboardMarkup,
              **data) -> None:
    """Вопрос админу: экран перерисовывается в вопрос на месте (повторное нажатие не плодит сообщения),
    id сообщения запоминается - после ответа вопрос убирается. Под фото/файлом - вопрос новым сообщением."""
    await state.set_state(st)
    shown = await show(call, text, kb)
    if shown is None:
        await state.update_data(**data)
        return
    await state.update_data(prompt_chat=shown.chat.id, prompt_mid=shown.message_id, **data)


async def ask_new(message: Message, state: FSMContext, st: State, text: str, kb: InlineKeyboardMarkup,
                  **data) -> None:
    """То же, но новым сообщением (когда исходное нельзя перерисовать)."""
    prompt = await message.answer(text, reply_markup=kb)
    await state.set_state(st)
    await state.update_data(prompt_chat=prompt.chat.id, prompt_mid=prompt.message_id, **data)


async def close_prompt(bot: Bot, data: dict, note: str | None = None) -> None:
    """Убирает вопрос после ответа: удаляет сообщение (или, если нельзя, снимает кнопки и пишет итог)."""
    chat, mid = data.get("prompt_chat"), data.get("prompt_mid")
    if not chat or not mid:
        return
    try:
        if note is None:
            await bot.delete_message(chat, mid)
        else:
            await bot.edit_message_text(note, chat_id=chat, message_id=mid, reply_markup=None)
    except TelegramBadRequest as e:
        log.debug("close_prompt: %s", e)
        try:
            await bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=None)
        except TelegramBadRequest:
            pass
