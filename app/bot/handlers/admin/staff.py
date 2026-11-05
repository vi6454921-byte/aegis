"""Админка → 👮 Админы (2 уровень): список, приглашение ссылкой, добавление по id/@username, уровни, снятие.

📌 Закреплённые админы 2 уровня (SENIOR_ADMIN_USERNAMES / SENIOR_ADMIN_IDS в .env) видны с меткой 📌:
«ожидает /start», пока ник не привязан к id. Снять или понизить закреплённого может только владелец —
он же может вернуть снятое закрепление кнопкой «📌 Вернуть закрепление»."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.commands import refresh_commands
from app.bot.handlers.admin.base import admin_router, ask, cancel_btn, close_prompt, menu_btn
from app.bot.keyboards import cb, ik, main_menu
from app.bot.notify import safe_send
from app.bot.states import AdminSG
from app.bot.ui import show
from app.config import Config
from app.db.models import PinnedAdmin, User
from app.services import pinned as pinned_svc
from app.services import roles
from app.services.users import display_name, find_user, short_name
from app.utils.timeutil import fmt_dt, now_utc

router = admin_router("admin_staff", level=2)


def S(a: str = "", uid: int = 0, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="st", a=a, id=uid, v=v)


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "st") & (F.a == action))


def _who(row: PinnedAdmin) -> str:
    return f"@{html.escape(row.username)}" if row.username else f"id <code>{row.user_id}</code>"


async def pinned_lines(session: AsyncSession, config: Config,
                       actor_id: int) -> tuple[list[str], list[list[InlineKeyboardButton]]]:
    """📌 из .env: кто ещё не привязан («ожидает /start») и чьё закрепление снял владелец (вернуть — кнопкой)."""
    lines: list[str] = []
    buttons: list[list[InlineKeyboardButton]] = []
    rows = {r.username: r for r in await pinned_svc.entries(session) if r.username}
    for name, shown in config.senior_usernames.items():
        row = rows.get(name)
        if row is None or (roles.bind_state(row) == "env"):
            lines.append(f"📌 @{html.escape(shown)} — 2 уровень · ожидает /start")
        elif roles.bind_state(row) == "owner":
            lines.append(f"📌 {_who(row)} — закрепление снято владельцем {fmt_dt(row.revoked_at)}")
            if roles.is_owner(config, actor_id):
                buttons.append([cb(f"📌 Вернуть закрепление: @{shown}", S("repin", row.id))])
    by_id = [r for r in await pinned_svc.entries(session) if r.source == "id" and r.user_id in config.senior_ids]
    for row in by_id:
        if roles.bind_state(row) == "owner":
            lines.append(f"📌 {_who(row)} — закрепление снято владельцем {fmt_dt(row.revoked_at)}")
            if roles.is_owner(config, actor_id):
                buttons.append([cb(f"📌 Вернуть закрепление: id {row.user_id}", S("repin", row.id))])
    return lines, buttons


def staff_icon(config: Config, uid: int, level: int) -> str:
    if config.is_pinned(uid):
        return "📌"
    return "🛡" if level >= 2 else "🔹"


async def list_view(session: AsyncSession, config: Config, actor_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows_db = await roles.staff_rows(session)
    lines = ["👮 <b>Админы</b>",
             "<i>1 уровень — заказы, чеки, обращения, задания, сообщения клиентам. 2 уровень — всё, включая "
             "настройки, деньги, баны и админов 1 уровня. 📌 — закреплён в .env: снять может только владелец.</i>", ""]
    for uid in sorted(config.admin_ids):
        lines.append(f"👑 {display_name(await session.get(User, uid))} — владелец (из .env)")
    buttons = []
    for uid, level in sorted(config.staff.items(), key=lambda kv: (-kv[1], not config.is_pinned(kv[0]), kv[0])):
        if uid in config.admin_ids:
            continue
        u = await session.get(User, uid)
        row = rows_db.get(uid)
        added = f" · с {fmt_dt(row.added_at)}" if row else ""
        icon = staff_icon(config, uid, level)
        who = display_name(u) if u else f"id <code>{uid}</code> (ещё не писал боту)"
        lines.append(f"{icon} {who} — {roles.LEVELS[level]}{added}")
        buttons.append([cb(f"{icon} {short_name(u) if u else uid} · {roles.LEVEL_SHORT[level]}", S("card", uid))])
    extra, repin = await pinned_lines(session, config, actor_id)
    lines += extra
    if len(lines) == 3:
        lines.append("Пока никого.")
    invites = [cb("➕ Пригласить: 1 уровень", S("invite", v="1"))]
    if roles.is_owner(config, actor_id):
        invites.append(cb("➕ 2 уровень", S("invite", v="2")))
    return "\n".join(lines), ik(*buttons, *repin, invites, [cb("🔍 Добавить по id / @username", S("add"))],
                                [menu_btn()])


async def card_view(session: AsyncSession, config: Config, actor_id: int, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    u = await session.get(User, uid)
    level = config.admin_level(uid)
    row = (await roles.staff_rows(session)).get(uid)
    by = await session.get(User, row.added_by) if row and row.added_by else None
    lines = [f"👮 {display_name(u) if u else f'id <code>{uid}</code>'}",
             f"Уровень: <b>{roles.LEVELS.get(level, 'не админ')}</b>"]
    if row and row.added_by:
        lines.append(f"Добавлен {fmt_dt(row.added_at)}" + (f" · {display_name(by)}" if by else ""))
    if config.is_pinned(uid):
        lines.append("📌 Закреплён в .env (SENIOR_ADMIN_USERNAMES / SENIOR_ADMIN_IDS). Снять или понизить может "
                     "только владелец — тогда закрепление снимается.")
    rows = []
    if roles.is_owner(config, actor_id) and level in (1, 2) and not roles.is_owner(config, uid):
        rows.append([cb("⬇️ Сделать 1 уровнем", S("lvl", uid, "1")) if level == 2
                     else cb("⬆️ Сделать 2 уровнем", S("lvl", uid, "2"))])
    if level and roles.can_manage(config, actor_id, uid, level):
        rows.append([cb("🗑 Снять доступ", S("rm", uid))])
    rows.append([cb("⬅️ К админам", S()), menu_btn()])
    return "\n".join(lines), ik(*rows)


async def add_view(session: AsyncSession, config: Config, actor_id: int, u: User,
                   only_level: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """«Сделать админом?» — по найденному пользователю (кнопки «👮 Админы», /addadmin, управление)."""
    if config.admin_level(u.id):
        text, kb = await card_view(session, config, actor_id, u.id)
        return "Он уже админ.\n\n" + text, kb
    if u.id == actor_id:
        return texts.ADMIN_CANT_SELF, ik([cb("⬅️ К админам", S()), menu_btn()])
    rows = []
    if only_level in (None, 1):
        rows.append([cb("🔹 Добавить: 1 уровень", S("grant", u.id, "1"))])
    if roles.is_owner(config, actor_id) and only_level in (None, 2):
        rows.append([cb("🛡 Добавить: 2 уровень", S("grant", u.id, "2"))])
    if not rows:
        return texts.OWNER_ONLY, ik([cb("⬅️ К админам", S()), menu_btn()])
    rows.append([cb(texts.BTN_CANCEL, S())])
    return f"Сделать админом {display_name(u)}?", ik(*rows)


async def remove_view(session: AsyncSession, config: Config, actor_id: int,
                      uid: int) -> tuple[str, InlineKeyboardMarkup]:
    u = await session.get(User, uid)
    level = config.admin_level(uid)
    if not level:
        return f"{display_name(u)} — не админ.", ik([cb("⬅️ К админам", S()), menu_btn()])
    if not roles.can_manage(config, actor_id, uid, level):
        text, kb = await card_view(session, config, actor_id, uid)
        return "⛔ Недостаточно прав, чтобы снять этого админа.\n\n" + text, kb
    note = "\n📌 Он закреплён в .env — закрепление тоже снимется." if config.is_pinned(uid) else ""
    return (f"Снять доступ к админке у {display_name(u)}?{note}",
            ik([cb("🗑 Да, снять", S("rm_go", uid)), cb(texts.BTN_CANCEL, S("card", uid))]))


@router.callback_query(AdminCb.filter((F.s == "st") & F.a.in_({"", "list"})))
async def st_list(call: CallbackQuery, state: FSMContext, session: AsyncSession, config: Config) -> None:
    await state.clear()
    await call.answer()
    text, kb = await list_view(session, config, call.from_user.id)
    await show(call, text, kb)


@router.callback_query(_f("card"))
async def st_card(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config) -> None:
    await call.answer()
    text, kb = await card_view(session, config, call.from_user.id, callback_data.id)
    await show(call, text, kb)


@router.callback_query(_f("invite"))
async def st_invite(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config,
                    bot_username: str) -> None:
    level = 2 if callback_data.v == "2" else 1
    try:
        token = await roles.create_invite(session, config, level, call.from_user.id, now_utc())
    except roles.RoleError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer()
    link = f"https://t.me/{bot_username}?start=adm_{token}"
    await show(call, (f"🔗 <b>Приглашение: {roles.LEVELS[level]}</b>\n\n<code>{html.escape(link)}</code>\n\n"
                      "Перешли ссылку человеку: он откроет бота и сразу станет админом. Ссылка одноразовая и "
                      f"действует {int(roles.INVITE_TTL.total_seconds() // 3600)} ч."),
               ik([cb("⬅️ К админам", S()), menu_btn()]))


@router.callback_query(_f("add"))
async def st_add(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminSG.staff_add,
              "🔍 Пришли id или @username человека. Он должен хотя бы раз открыть бота (/start).",
              ik([cancel_btn(S())]))


@router.message(AdminSG.staff_add)
async def st_add_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                       config: Config) -> None:
    u, error = await find_user(session, message.text)
    if u is None:
        await message.answer(error, reply_markup=ik([cancel_btn(S())]))
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    text, kb = await add_view(session, config, message.from_user.id, u)
    await message.answer(text, reply_markup=kb)


async def _welcome(bot: Bot, config: Config, uid: int, level: int) -> None:
    await refresh_commands(bot, config, uid)
    await safe_send(bot, uid, texts.ADMIN_INVITE_OK.format(level=roles.LEVELS[level]), reply_markup=main_menu(True))


@router.callback_query(_f("grant"))
async def st_grant(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                   config: Config) -> None:
    level = 2 if callback_data.v == "2" else 1
    if callback_data.id == call.from_user.id:
        await call.answer(texts.ADMIN_CANT_SELF, show_alert=True)
        return
    if await session.get(User, callback_data.id) is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    try:
        await roles.set_level(session, config, callback_data.id, level, call.from_user.id, now_utc())
    except roles.RoleError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Готово ✅")
    await _welcome(bot, config, callback_data.id, level)
    text, kb = await card_view(session, config, call.from_user.id, callback_data.id)
    await show(call, "✅ Добавлен\n\n" + text, kb)


@router.callback_query(_f("lvl"))
async def st_level(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                   config: Config) -> None:
    level = 2 if callback_data.v == "2" else 1
    try:
        await roles.set_level(session, config, callback_data.id, level, call.from_user.id, now_utc())
    except roles.RoleError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Уровень изменён")
    await refresh_commands(bot, config, callback_data.id)
    await safe_send(bot, callback_data.id, f"🛡 Твой уровень в админке теперь: {roles.LEVELS[level]}.")
    text, kb = await card_view(session, config, call.from_user.id, callback_data.id)
    await show(call, text, kb)


@router.callback_query(_f("rm"))
async def st_remove_ask(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config) -> None:
    await call.answer()
    await show(call, *await remove_view(session, config, call.from_user.id, callback_data.id))


@router.callback_query(_f("rm_go"))
async def st_remove(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                    bot: Bot, config: Config) -> None:
    uid = callback_data.id
    try:
        await roles.remove(session, config, uid, call.from_user.id)
    except roles.RoleError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Доступ снят")
    key = StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)
    other = FSMContext(storage=state.storage, key=key)
    if (await other.get_state() or "").startswith("AdminSG"):
        await other.clear()
    await refresh_commands(bot, config, uid)
    await safe_send(bot, uid, texts.ADMIN_REMOVED_NOTICE, reply_markup=main_menu(False))
    text, kb = await list_view(session, config, call.from_user.id)
    await show(call, "🗑 Доступ снят\n\n" + text, kb)


@router.callback_query(_f("repin"))
async def st_repin(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                   config: Config) -> None:
    """Владелец возвращает закрепление, которое сам снял (ник или id всё ещё в .env)."""
    if not roles.is_owner(config, call.from_user.id):
        await call.answer(texts.OWNER_ONLY, show_alert=True)
        return
    try:
        uid = await pinned_svc.restore(session, config, callback_data.id, call.from_user.id, now_utc())
    except roles.RoleError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer("Закрепление возвращено 📌")
    await refresh_commands(bot, config, uid)
    await safe_send(bot, uid, texts.PINNED_WELCOME, reply_markup=main_menu(True))
    text, kb = await list_view(session, config, call.from_user.id)
    await show(call, "📌 Закрепление возвращено\n\n" + text, kb)
