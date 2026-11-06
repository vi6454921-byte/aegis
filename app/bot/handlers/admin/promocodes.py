"""Админка 2 уровня → «🔐 Управление» → «🎟 Промокоды»: коды на безлимит и сам безлимит у пользователей.

Список кодов со статусами (свободен / активирован кем и когда / отозван), фильтры, карточка кода, отзыв
(если код - текущий безлимит пользователя, безлимит снимается и клиенту приходит уведомление), выгрузка в txt
(тот же формат, что promo_codes.txt), выпуск новых кодов, ручная выдача и снятие безлимита.
Все действия - в admin_log. У админов 1 уровня этих кнопок нет, а старые нажатия отсекает фильтр уровня."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.base import admin_router, ask, cancel_btn, close_prompt, menu_btn
from app.bot.keyboards import cb, ik, pager
from app.bot.notify import safe_send
from app.bot.render import unlimited_status
from app.bot.states import AdminPromoSG
from app.bot.ui import show
from app.db.models import PromoCode, User
from app.services import promocodes as pc
from app.services.admin_log import log_admin
from app.services.users import display_name, find_user
from app.utils.timeutil import fmt_dt, full_date, now_utc

router = admin_router("admin_promocodes", level=2)
PER_PAGE = 10
View = tuple[str, InlineKeyboardMarkup]


def P(a: str = "", code_id: int = 0, v: str = "", p: int = 0) -> AdminCb:  # noqa: N802
    return AdminCb(s="pc", a=a, id=code_id, v=v, p=p)


def _on(*actions: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "pc") & F.a.in_(set(actions)))


def back_btn() -> object:
    return cb(texts.PC_BACK_BTN, P())


async def status_of(session: AsyncSession, code: PromoCode) -> str:
    if code.revoked_at is not None:
        return texts.PC_REVOKED.format(date=fmt_dt(code.revoked_at))
    if code.used_by is not None:
        user = await session.get(User, code.used_by)
        return texts.PC_USED.format(user=display_name(user) if user else f"id {code.used_by}",
                                    date=fmt_dt(code.used_at))
    return texts.PC_FREE


def _icon(code: PromoCode) -> str:
    return "⛔" if code.revoked_at else "✅" if code.used_by else "🟢"


# ------------------------------------------------------------------ экраны
async def hub_view(session: AsyncSession, flt: str = "all", page: int = 0) -> View:
    if flt not in texts.PC_FILTERS:
        flt = "all"
    c = await pc.counts(session)
    active = len(await pc.unlimited_users(session, now_utc()))
    rows, total = await pc.page(session, max(page, 0), PER_PAGE, flt)
    lines = [texts.PC_TITLE.format(active=active, **c), ""]
    buttons = []
    for code in rows:
        lines.append(f"{_icon(code)} <code>{code.code}</code> · {pc.kind_label(code)} · "
                     f"{html.escape(await status_of(session, code))}")
        buttons.append(cb(f"{_icon(code)} {code.code}", P("code", code.id)))
    if not rows:
        lines.append(texts.PC_LIST_EMPTY)
    grid = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    filters = [cb(("• " if k == flt else "") + name + (f" · {c[k]}" if c.get(k) else ""), P(v=k))
               for k, name in texts.PC_FILTERS.items()]
    text = "\n".join(lines)
    return (text if len(text) <= 4000 else text[:3990] + "…"), ik(
        *grid, pager(page, total, PER_PAGE, lambda p: P(v=flt, p=p)), filters[:2], filters[2:],
        [cb(texts.PC_DOWNLOAD_BTN, P("dl")), cb(texts.PC_GENERATE_BTN, P("gen"))],
        [cb(texts.PC_FIND_BTN, P("find")), cb(texts.PC_USERS_BTN, P("users"))],
        [cb(texts.PC_GRANT_BTN, P("ask_user", v="grant")), cb(texts.PC_REMOVE_BTN, P("ask_user", v="remove"))],
        [cb(texts.PC_MANAGE_BACK_BTN, AdminCb(s="g")), menu_btn()])


async def code_view(session: AsyncSession, code: PromoCode) -> View:
    by = ""
    if code.created_by:
        admin = await session.get(User, code.created_by)
        by = texts.PC_CARD_BY.format(admin=display_name(admin) if admin else code.created_by)
    text = texts.PC_CARD.format(code=code.code, kind=pc.kind_label(code), status=await status_of(session, code),
                                created=fmt_dt(code.created_at), by=by)
    rows = []
    if code.revoked_at is None:
        rows.append([cb(texts.PC_REVOKE_BTN, P("revoke", code.id))])
    if code.used_by:
        user = await session.get(User, code.used_by)
        if user is not None:
            text += texts.PC_CARD_CLIENT_NOW.format(status=unlimited_status(user) or texts.PC_NO_UNLIMITED)
        rows.append([cb(texts.PC_CLIENT_BTN, AdminCb(s="u", a="card", id=code.used_by)),
                     cb(texts.PC_CLIENT_UNLIMITED_BTN, P("user", code.used_by))])
    rows.append([back_btn(), menu_btn()])
    return text, ik(*rows)


async def user_view(session: AsyncSession, user: User, note: str = "") -> View:
    now = now_utc()
    status = unlimited_status(user, now)
    source = ""
    if status and user.unlimited_code_id:
        code = await session.get(PromoCode, user.unlimited_code_id)
        source = texts.PC_SOURCE_CODE.format(code=code.code) if code else ""
    elif status and user.unlimited_by:
        admin = await session.get(User, user.unlimited_by)
        source = texts.PC_SOURCE_ADMIN.format(admin=display_name(admin) if admin else user.unlimited_by)
    text = texts.PC_GRANT_ASK.format(user=display_name(user),
                                     now=texts.PC_NOW.format(status=f"{status}{source}" if status
                                                                else texts.PC_NO_UNLIMITED))
    if note:
        text = f"{note}\n\n{text}"
    rows = [[cb(texts.PC_GRANT_FOREVER_BTN, P("grant_go", user.id, "forever")),
             cb(texts.PC_GRANT_DAYS_BTN.format(days=pc.DAYS_DEFAULT), P("grant_go", user.id, str(pc.DAYS_DEFAULT)))]]
    if status:
        rows.append([cb(texts.PC_REMOVE_BTN, P("remove", user.id))])
    rows.append([cb(texts.PC_USER_CARD_BTN, AdminCb(s="u", a="card", id=user.id)), back_btn()])
    return text, ik(*rows)


def _file(codes: list[PromoCode], name: str = "promo_codes.txt") -> BufferedInputFile:
    return BufferedInputFile(pc.export_txt(codes).encode("utf-8"), filename=name)


# ------------------------------------------------------------------ список, карточка, выгрузка
@router.callback_query(_on(""))
async def pc_hub(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    text, kb = await hub_view(session, callback_data.v or "all", callback_data.p)
    await show(call, text, kb)


@router.callback_query(_on("code"))
async def pc_code(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    code = await session.get(PromoCode, callback_data.id, populate_existing=True)
    if code is None:
        await call.answer(texts.PC_NOT_FOUND, show_alert=True)
        return
    await state.clear()
    await call.answer()
    text, kb = await code_view(session, code)
    await show(call, text, kb)


@router.callback_query(_on("dl"))
async def pc_download(call: CallbackQuery, session: AsyncSession) -> None:
    codes = await pc.all_codes(session)
    log_admin(session, call.from_user.id, "promo_export", "promo_code", None, count=len(codes))
    await session.commit()
    await call.answer()
    await call.message.answer_document(_file(codes), caption=texts.PC_FILE_CAPTION)


# ------------------------------------------------------------------ отзыв кода
@router.callback_query(_on("revoke"))
async def pc_revoke(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    code = await session.get(PromoCode, callback_data.id, populate_existing=True)
    if code is None or code.revoked_at is not None:
        await call.answer(texts.PC_ALREADY_REVOKED if code else texts.PC_NOT_FOUND, show_alert=True)
        return
    extra = ""
    if code.used_by:
        user = await session.get(User, code.used_by, populate_existing=True)
        if user is not None and user.unlimited_code_id == code.id and user.unlimited_active(now_utc()):
            extra = texts.PC_REVOKE_EXTRA.format(user=display_name(user))
    await call.answer()
    await show(call, texts.PC_REVOKE_CONFIRM.format(code=code.code, extra=extra),
               ik([cb(texts.PC_REVOKE_YES, P("revoke_go", code.id)), cb(texts.BTN_CANCEL, P("code", code.id))]))


@router.callback_query(_on("revoke_go"))
async def pc_revoke_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    try:
        res = await pc.revoke(session, callback_data.id, call.from_user.id, now_utc())
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return
    extra = ""
    if res.user is not None:
        await safe_send(bot, res.user.id, texts.PROMO_REVOKED_NOTICE)
        extra = texts.PC_REVOKED_USER_OFF.format(user=display_name(res.user))
    await call.answer(texts.PC_REVOKED_TOAST)
    text, kb = await code_view(session, res.code)
    await show(call, texts.PC_REVOKED_DONE.format(code=res.code.code, extra=extra) + "\n\n" + text, kb)


# ------------------------------------------------------------------ выпуск новых
@router.callback_query(_on("gen"))
async def pc_gen(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, texts.PC_GEN_ASK_KIND,
               ik([cb(texts.PC_GEN_FOREVER_BTN, P("gen_kind", v="forever")),
                   cb(texts.PC_GEN_DAYS_BTN.format(days=pc.DAYS_DEFAULT), P("gen_kind", v="days"))],
                  [cancel_btn(P())]))


def _kind_name(kind: str) -> str:
    return texts.PC_KIND_FOREVER if kind == "forever" else texts.PC_KIND_DAYS.format(days=pc.DAYS_DEFAULT)


@router.callback_query(_on("gen_kind"))
async def pc_gen_kind(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    kind = "forever" if callback_data.v == "forever" else "days"
    await call.answer()
    await ask(call, state, AdminPromoSG.gen_count,
              texts.PC_GEN_ASK_COUNT.format(kind=_kind_name(kind), max=pc.GENERATE_MAX),
              ik([cb(str(n), P("gen_go", n, kind)) for n in (5, 10, 20)], [cancel_btn(P())]), kind=kind)


async def _generate(message: Message, session: AsyncSession, admin_id: int, kind: str, n: int) -> None:
    made = await pc.generate(session, n, kind, pc.DAYS_DEFAULT if kind == "days" else None, admin_id, now_utc())
    await message.answer_document(_file(made, "promo_codes_new.txt"),
                                  caption=texts.PC_GEN_DONE.format(n=len(made), kind=_kind_name(kind)))
    text, kb = await hub_view(session, "free")
    await message.answer(text, reply_markup=kb)


@router.callback_query(_on("gen_go"))
async def pc_gen_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                    bot: Bot) -> None:
    kind, n = callback_data.v, callback_data.id  # кнопки «5 / 10 / 20»: v - тип, id - сколько
    if not 1 <= n <= pc.GENERATE_MAX:
        await call.answer(texts.PC_GEN_BAD.format(max=pc.GENERATE_MAX), show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    await call.answer()
    await _generate(call.message, session, call.from_user.id, "forever" if kind == "forever" else "days", n)


@router.message(AdminPromoSG.gen_count)
async def pc_gen_count(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    t = (message.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= pc.GENERATE_MAX:
        await message.answer(texts.PC_GEN_BAD.format(max=pc.GENERATE_MAX), reply_markup=ik([cancel_btn(P())]))
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    await _generate(message, session, message.from_user.id, str(data.get("kind") or "days"), int(t))


# ------------------------------------------------------------------ поиск кода
@router.callback_query(_on("find"))
async def pc_find(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminPromoSG.find_code, texts.PC_FIND_ASK, ik([cancel_btn(P())]))


@router.message(AdminPromoSG.find_code)
async def pc_find_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    code = await pc.find(session, message.text)
    if code is None:  # ввод остаётся открытым - можно прислать другой
        await message.answer(texts.PC_NOT_FOUND, reply_markup=ik([cancel_btn(P())]))
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    text, kb = await code_view(session, code)
    await message.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ безлимит у пользователей
@router.callback_query(_on("users"))
async def pc_users(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    users = await pc.unlimited_users(session, now_utc())
    lines = [texts.PC_USERS_TITLE.format(n=len(users))]
    buttons = []
    for u in users[:40]:
        lines.append(f"• {display_name(u)}: {unlimited_status(u)}")
        if len(buttons) < 20:
            buttons.append([cb(f"♾ {u.username and '@' + u.username or u.first_name or u.id}"[:40], P("user", u.id))])
    if not users:
        lines.append(texts.PC_LIST_EMPTY)
    await call.answer()
    text = "\n".join(lines)
    await show(call, text if len(text) <= 4000 else text[:3990] + "…", ik(*buttons, [back_btn(), menu_btn()]))


@router.callback_query(_on("user"))
async def pc_user(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    user = await session.get(User, callback_data.id, populate_existing=True)
    if user is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await state.clear()
    await call.answer()
    text, kb = await user_view(session, user)
    await show(call, text, kb)


@router.callback_query(_on("ask_user"))
async def pc_ask_user(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    what = "remove" if callback_data.v == "remove" else "grant"
    await call.answer()
    await ask(call, state, AdminPromoSG.user,
              texts.PC_USER_ASK.format(what=texts.PC_REMOVE_WHAT if what == "remove" else texts.PC_GRANT_WHAT),
              ik([cancel_btn(P())]), what=what)


async def _remove_confirm(session: AsyncSession, user: User) -> View:
    status = unlimited_status(user)
    if not status:
        return texts.PC_REMOVE_NONE.format(user=display_name(user)), ik([back_btn(), menu_btn()])
    return (texts.PC_REMOVE_CONFIRM.format(user=display_name(user), status=status),
            ik([cb(texts.PC_REMOVE_YES, P("remove_go", user.id)), cb(texts.BTN_CANCEL, P("user", user.id))]))


@router.message(AdminPromoSG.user)
async def pc_user_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    user, error = await find_user(session, message.text)
    if user is None:
        await message.answer(error, reply_markup=ik([cancel_btn(P())]))
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    text, kb = await (_remove_confirm(session, user) if data.get("what") == "remove" else user_view(session, user))
    await message.answer(text, reply_markup=kb)


@router.callback_query(_on("remove"))
async def pc_remove(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    user = await session.get(User, callback_data.id, populate_existing=True)
    if user is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    text, kb = await _remove_confirm(session, user)
    await show(call, text, kb)


@router.callback_query(_on("grant_go"))
async def pc_grant_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    days = None if callback_data.v == "forever" else int(callback_data.v or pc.DAYS_DEFAULT)
    try:
        user = await pc.grant(session, callback_data.id, days, call.from_user.id, now_utc())
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return
    await safe_send(bot, user.id, texts.UNLIMITED_GRANTED_FOREVER if days is None
                    else texts.UNLIMITED_GRANTED_UNTIL.format(date=full_date(user.unlimited_until)))
    await call.answer(texts.PC_GRANTED_TOAST)
    text, kb = await user_view(session, user, texts.PC_GRANT_DONE.format(user=display_name(user),
                                                                       status=unlimited_status(user)))
    await show(call, text, kb)


@router.callback_query(_on("remove_go"))
async def pc_remove_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    user = await session.get(User, callback_data.id, populate_existing=True)
    if user is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    if not unlimited_status(user):
        await call.answer(texts.PC_REMOVE_NONE.format(user=user.first_name or user.id), show_alert=True)
        return
    user = await pc.remove(session, user.id, call.from_user.id)
    await safe_send(bot, user.id, texts.UNLIMITED_REMOVED_NOTICE)
    await call.answer(texts.PC_REMOVED_TOAST)
    text, kb = await user_view(session, user, texts.PC_REMOVE_DONE.format(user=display_name(user)))
    await show(call, text, kb)
