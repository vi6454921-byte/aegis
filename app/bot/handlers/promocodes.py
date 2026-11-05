"""Промокод на безлимит у клиента: «🎟 Ввести промокод» (главное меню и «🎁 Бонусы и акции»), команда
/promo [КОД] и просто код сообщением (AEGIS-XXXX-XXXX) вне сценариев.

Сама проверка — app.services.promocodes.activate: один код — один аккаунт (атомарно), не больше 5 неверных
попыток в час, код «на 30 дней» не продлевает действующий безлимит, «навсегда» заменяет 30-дневный."""
from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb, CodeCb
from app.bot.handlers.common import menu_router
from app.bot.keyboards import cb, ik, main_menu, order_alert_kb
from app.bot.notify import mark_headers, notify_admins
from app.bot.states import PromoCodeSG
from app.bot.ui import drop_kb
from app.config import Config
from app.db.models import User
from app.services import promocodes as pc
from app.services.users import display_name
from app.utils.timeutil import full_date, now_utc, to_local

router = Router(name="promocodes")
M = texts.MAIN_MENU
_SEP = re.compile(r"[\s\-–—_.]+")


def plain_code(text: str | None) -> bool:
    """Вне сценариев кодом считаем только «AEGIS…» или «XXXX-XXXX» — чтобы обычное слово не стало попыткой."""
    t = (text or "").strip()
    if not t or len(t) > 40 or "\n" in t:
        return False
    compact = _SEP.sub("", t)
    if t.upper().startswith("AEGIS"):
        return compact.isascii() and compact.isalnum() and len(compact) >= 9
    return bool(re.fullmatch(r"[A-Za-z0-9]{4}[\-–—][A-Za-z0-9]{4}", t)) and pc.looks_like_code(t)


async def ask_code(message: Message, state: FSMContext) -> None:
    await state.set_state(PromoCodeSG.code)
    await message.answer(texts.PROMO_PROMPT, reply_markup=ik([cb(texts.BTN_CANCEL, CodeCb(action="cancel"))]))


async def activate_and_reply(message: Message, session: AsyncSession, user: User, code_text: str, bot: Bot,
                             config: Config) -> pc.Activation:
    now = now_utc()
    before = pc.kind_of(user, now)
    res = await pc.activate(session, user.id, code_text, now)
    if res.ok:
        text = texts.PROMO_OK_FOREVER if res.status == "forever" else texts.PROMO_OK_UNTIL.format(
            date=full_date(res.until))
        if res.status == "forever" and before == "days":
            text += texts.PROMO_OK_REPLACED
        text += texts.PROMO_OK_DETAILS
        if res.paid_orders:
            text += texts.PROMO_OK_PAID.format(ids=", ".join(f"#{i}" for i in res.paid_orders))
        await message.answer(text, reply_markup=main_menu())
        fresh = await session.get(User, user.id, populate_existing=True)
        for oid in res.paid_orders or []:  # заказ, ждавший оплаты, теперь в очереди — сообщаем команде
            await mark_headers(bot, session, config, "order", oid,
                               texts.ADMIN_PROMO_MARK.format(time=f"{to_local(now):%H:%M}"),
                               markup_for=lambda _lvl, o=oid: order_alert_kb(o))
            await notify_admins(bot, config, texts.ADMIN_PROMO_PAID.format(user=display_name(fresh), id=oid),
                                markup_for=lambda _lvl, o=oid: ik([cb(texts.ADMIN_PROMO_OPEN_BTN,
                                                                      AdminCb(s="o", a="open", id=o))]))
        await session.commit()
        return res
    if res.status == "mine":
        text = texts.PROMO_MINE
    elif res.status == "has_forever":
        text = texts.PROMO_HAS_FOREVER
    elif res.status == "has_days":
        text = texts.PROMO_HAS_DAYS.format(date=full_date(res.until))
    elif res.status == "blocked":
        text = texts.PROMO_BLOCKED.format(minutes=res.wait_minutes)
    else:
        fails, _ = await pc.fails_in_window(session, user.id, now)
        left = max(pc.MAX_FAILS - fails, 0)
        if left == 0:
            text = texts.PROMO_BLOCKED.format(minutes=await pc.blocked_for(session, user.id, now) or 60)
            res.status = "blocked"
        else:
            tmpl = {"used": texts.PROMO_USED, "revoked": texts.PROMO_REVOKED}.get(res.status, texts.PROMO_INVALID)
            text = tmpl.format(left=left)
    await message.answer(text)
    return res


# ------------------------------------------------------------------ вход
@menu_router.message(F.text == M["promo"])
async def menu_promo(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ask_code(message, state)


@menu_router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                    db_user: User, bot: Bot, config: Config) -> None:
    await state.clear()
    if (command.args or "").strip():
        res = await activate_and_reply(message, session, db_user, command.args, bot, config)
        if res.status in ("invalid", "used", "revoked"):  # можно сразу прислать другой код
            await state.set_state(PromoCodeSG.code)
        return
    await ask_code(message, state)


@router.callback_query(CodeCb.filter(F.action == "enter"))
async def cb_enter(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await ask_code(call.message, state)


@router.callback_query(CodeCb.filter(F.action == "cancel"))
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await drop_kb(call)
    await call.message.answer(texts.PROMO_CODE_CANCELLED, reply_markup=main_menu())


# ------------------------------------------------------------------ ввод кода
@router.message(PromoCodeSG.code)
async def code_input(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                     config: Config) -> None:
    if not (message.text or "").strip():
        await message.answer(texts.PROMO_EMPTY, reply_markup=ik([cb(texts.BTN_CANCEL, CodeCb(action="cancel"))]))
        return
    res = await activate_and_reply(message, session, db_user, message.text, bot, config)
    if res.status not in ("invalid", "used", "revoked"):  # при ошибке ввод остаётся открытым
        await state.clear()


@router.message(StateFilter(None), F.text.func(plain_code))
async def code_plain(message: Message, session: AsyncSession, db_user: User, bot: Bot, config: Config) -> None:
    """Прислал код просто сообщением — активируем без лишних кнопок."""
    await activate_and_reply(message, session, db_user, message.text, bot, config)
