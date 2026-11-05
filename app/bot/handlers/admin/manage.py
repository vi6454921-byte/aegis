"""Админка → 🔐 Управление (2 уровень) и текстовые команды админов.

Блок на главном экране /admin (видит и может нажать только 2 уровень): 👮 Админы (staff.py),
⭐ Приоритет / 🎟 Месяц бесплатно, 🚫 Бан / разбан, 💸 Деньги, 🗄 Запросы к БД (dbq.py), 📊 Статистика,
📣 Рассылка, ⚙️ Настройки, 💾 Данные. Пользователя ищем по @username или ID (services.users.find_user).
Каждое действие — с подтверждением, уведомлением пользователю и записью в журнал админов (admin_log).

Права проверяет сервер на каждом нажатии и каждой команде: роутер l2 — IsAdmin(2), l1 — IsAdmin(1).
Команды 2 уровня (/give /ban /unban /priority /freemonth /addadmin /removeadmin /db /backup) и быстрое
«@username +500» от админа 1 уровня получают «Недостаточно прав». Без аргументов команда открывает экран.
"""
from __future__ import annotations

import html
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.commands import L2_COMMAND_NAMES
from app.bot.filters import IsLevel
from app.bot.handlers.admin import dbq, staff, users
from app.bot.handlers.admin.base import admin_router, ask, ask_new, cancel_btn, close_prompt, menu_btn
from app.bot.keyboards import cb, ik, main_menu
from app.bot.notify import safe_send
from app.bot.states import AdminSG
from app.bot.ui import show
from app.config import Config
from app.db.models import User
from app.services import admin_money
from app.services.admin_log import log_admin
from app.services.settings import RuntimeSettings
from app.services.users import display_name, find_user, free_month_active, search_users, short_name
from app.utils.money import INT64_MAX, fmt_rub, split_amount
from app.utils.timeutil import fmt_date, fmt_dt, now_utc

l2 = admin_router("admin_manage", level=2)
l1 = admin_router("admin_manage_l1", level=1)
router = Router(name="admin_manage_all")
router.include_routers(l2, l1)
View = tuple[str, InlineKeyboardMarkup]

DAYS_PRESETS = (7, 14, 30, 60, 90)
TASKS_PRESETS = (3, 5, 10, 20, 50)
MAX_DAYS, MAX_TASKS = 3650, 1000
# «@username +500», «@username -99,90 за ошибку» — знак обязателен, чтобы случайный текст не стал операцией
QUICK_RE = re.compile(r"^\s*@([A-Za-z0-9_]{3,32})\s+([+\-−–]\s*\d.*)$", re.DOTALL)
FIND_HEAD = {"vip": texts.VIP_TITLE, "bn": texts.BAN_TITLE, "gv": "💸 <b>Деньги</b>",
             "dbu": "👤 <b>Отчёт по пользователю</b>"}


def V(a: str = "", uid: int = 0, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="vip", a=a, id=uid, v=v)


def B(a: str = "", uid: int = 0, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="bn", a=a, id=uid, v=v)


def G(a: str = "", uid: int = 0, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="gv", a=a, id=uid, v=v)


def _on(section: str, *actions: str):  # noqa: ANN202
    return AdminCb.filter((F.s == section) & F.a.in_(set(actions)))


def card(uid: int) -> AdminCb:
    return AdminCb(s="u", a="card", id=uid)


def hub_btn() -> InlineKeyboardButton:
    return cb("⬅️ Управление", AdminCb(s="g"))


def hub_rows() -> list[list[InlineKeyboardButton]]:
    """Кнопки блока «🔐 Управление (2 уровень)» — на главном экране админки и на экране самого блока."""
    return [[cb("👮 Админы", AdminCb(s="st")), cb("⭐ Приоритет / 🎟 Месяц", V())],
            [cb("🚫 Бан / разбан", B()), cb("💸 Деньги", G())],
            [cb("🗄 Запросы к БД", AdminCb(s="db")), cb("📊 Статистика", AdminCb(s="m", a="stats"))],
            [cb("📣 Рассылка", AdminCb(s="b")), cb("⚙️ Настройки", AdminCb(s="s"))],
            [cb("💾 Данные", AdminCb(s="d")), cb(texts.PC_HUB_BTN, AdminCb(s="pc"))]]


def hub_view() -> View:
    return f"{texts.MANAGE_TITLE}\n{texts.MANAGE_INTRO}", ik(*hub_rows(), [menu_btn()])


async def _user(session: AsyncSession, uid: int) -> User | None:
    return await session.get(User, uid, populate_existing=True) if uid else None


async def _reply(message: Message, view: View | str) -> None:
    if isinstance(view, str):
        await message.answer(view)
    else:
        await message.answer(view[0], reply_markup=view[1])


# ------------------------------------------------------------------ ⭐ Приоритет и 🎟 «Месяц бесплатно»
def vip_hub_view() -> View:
    return (f"{texts.VIP_TITLE}\n{texts.VIP_INTRO}",
            ik([cb("🔍 Найти пользователя", V("find"))], [hub_btn(), menu_btn()]))


async def vip_user_view(session: AsyncSession, uid: int, note: str = "") -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    now = now_utc()
    fm = free_month_active(u, now)
    lines = [texts.VIP_TITLE, display_name(u), "",
             ("⭐ Приоритет: <b>есть</b>" + (" (за друзей)" if u.priority_auto else " (вручную)")) if u.is_priority
             else "⭐ Приоритет: нет",
             f"🎟 Месяц бесплатно: <b>до {fmt_date(u.free_month_until)}</b>, задач осталось {u.free_month_tasks_left}"
             if fm else "🎟 Месяц бесплатно: нет"]
    fm_row = [cb("🎟 Изменить месяц" if fm else "🎟 Подключить месяц", V("fm", u.id))]
    if fm:
        fm_row.append(cb("🎟 Отключить", V("fmoff", u.id)))
    kb = ik([cb("⭐ Снять приоритет" if u.is_priority else "⭐ Выдать приоритет", V("pr", u.id))], fm_row,
            [cb("👤 Карточка", card(u.id)), cb("🔍 Другой пользователь", V("find"))], [hub_btn(), menu_btn()])
    return (f"{note}\n\n" if note else "") + "\n".join(lines), kb


async def priority_confirm_view(session: AsyncSession, uid: int) -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    on = not u.is_priority
    text = (texts.VIP_PRIORITY_CONFIRM_ON if on else texts.VIP_PRIORITY_CONFIRM_OFF).format(user=display_name(u))
    return text, ik([cb(texts.GIVE_CONFIRM_BTN, V("pr_go", u.id, "on" if on else "off"))],
                    [cb(texts.BTN_CANCEL, V("user", u.id))])


async def days_view(session: AsyncSession, uid: int, s: RuntimeSettings) -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    default = int(s.free_month_days)
    presets = sorted({*DAYS_PRESETS, default} if 1 <= default <= MAX_DAYS else set(DAYS_PRESETS))
    btns = [cb(f"{d} дн." + (" ✓" if d == default else ""), V("fd", u.id, str(d))) for d in presets]
    btns.append(cb("✏️ Своё число", V("fdn", u.id)))
    return (texts.VIP_DAYS_ASK.format(user=display_name(u)),
            ik(btns[:3], btns[3:6], btns[6:], [cb(texts.BTN_CANCEL, V("user", u.id))]))


async def tasks_view(session: AsyncSession, uid: int, days: int, s: RuntimeSettings) -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    default = int(s.free_month_tasks)
    presets = sorted({*TASKS_PRESETS, default} if 1 <= default <= MAX_TASKS else set(TASKS_PRESETS))
    btns = [cb(f"{t}" + (" ✓" if t == default else ""), V("ft", u.id, f"{days}.{t}")) for t in presets]
    btns.append(cb("✏️ Своё число", V("ftn", u.id, str(days))))
    return (texts.VIP_TASKS_ASK.format(user=display_name(u), days=days),
            ik(btns[:3], btns[3:6], btns[6:], [cb(texts.BTN_CANCEL, V("user", u.id))]))


async def fm_confirm_view(session: AsyncSession, uid: int, days: int, tasks: int) -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    return (texts.VIP_FM_CONFIRM.format(user=display_name(u), days=days, tasks=tasks),
            ik([cb(texts.GIVE_CONFIRM_BTN, V("fm_go", u.id, f"{days}.{tasks}"))],
               [cb(texts.BTN_CANCEL, V("user", u.id))]))


async def fm_off_view(session: AsyncSession, uid: int) -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    return (texts.VIP_FM_OFF_CONFIRM.format(user=display_name(u)),
            ik([cb(texts.GIVE_CONFIRM_BTN, V("fmoff_go", u.id))], [cb(texts.BTN_CANCEL, V("user", u.id))]))


def _days(v: str) -> int | None:
    return int(v) if v.isdigit() and 1 <= int(v) <= MAX_DAYS else None


def _pair(v: str) -> tuple[int, int] | None:
    d, _, t = v.partition(".")
    days = _days(d)
    if days is None or not t.isdigit() or not 1 <= int(t) <= MAX_TASKS:
        return None
    return days, int(t)


async def set_priority(session: AsyncSession, bot: Bot, uid: int, on: bool, admin_id: int) -> str:
    u = await _user(session, uid)
    if u is None:
        return texts.NOT_FOUND
    if u.is_priority == on:
        return "ℹ️ Уже так: " + ("приоритет есть." if on else "приоритета нет.")
    u.is_priority, u.priority_auto = on, False
    log_admin(session, admin_id, "set_priority", "user", u.id, value=on)
    await session.commit()
    await safe_send(bot, u.id, texts.VIP_PRIORITY_ON_USER if on else texts.VIP_PRIORITY_OFF_USER)
    return "✅ «Приоритет» выдан." if on else "✅ «Приоритет» снят."


async def set_free_month(session: AsyncSession, bot: Bot, uid: int, days: int, tasks: int, admin_id: int) -> str:
    u = await _user(session, uid)
    if u is None:
        return texts.NOT_FOUND
    u.free_month_until = now_utc() + timedelta(days=days)
    u.free_month_tasks_left = tasks
    log_admin(session, admin_id, "set_free_month", "user", u.id, value=True, days=days, tasks=tasks,
              until=u.free_month_until.isoformat())
    await session.commit()
    await safe_send(bot, u.id, texts.VIP_FM_ON_USER.format(date=fmt_date(u.free_month_until), tasks=tasks))
    return f"✅ «Месяц бесплатно» подключён: до {fmt_date(u.free_month_until)}, задач {tasks}."


async def clear_free_month(session: AsyncSession, bot: Bot, uid: int, admin_id: int) -> str:
    u = await _user(session, uid)
    if u is None:
        return texts.NOT_FOUND
    if not free_month_active(u, now_utc()):
        return "ℹ️ «Месяц бесплатно» и так не действует."
    u.free_month_until, u.free_month_tasks_left = None, 0
    log_admin(session, admin_id, "set_free_month", "user", u.id, value=False)
    await session.commit()
    await safe_send(bot, u.id, texts.VIP_FM_OFF_USER)
    return "✅ «Месяц бесплатно» отключён."


# ------------------------------------------------------------------ 🚫 Бан и разбан
async def ban_hub_view(session: AsyncSession) -> View:
    total = int(await session.scalar(select(func.count(User.id)).where(User.is_banned.is_(True))) or 0)
    banned = list((await session.execute(select(User).where(User.is_banned.is_(True))
                                         .order_by(User.banned_at.desc()).limit(10))).scalars())
    lines = [texts.BAN_TITLE, texts.BAN_INTRO]
    if total:
        lines.append(f"\nЗабанены сейчас: <b>{total}</b>" + (" — последние кнопками ниже." if banned else ""))
    rows = [[cb("🔍 Найти пользователя", B("find"))]]
    rows += [[cb(f"🚫 {short_name(u)} · {fmt_date(u.banned_at)}", B("user", u.id))] for u in banned]
    rows.append([hub_btn(), menu_btn()])
    return "\n".join(lines), ik(*rows)


def ban_refusal(config: Config, actor_id: int, uid: int) -> str | None:
    if uid == actor_id:
        return texts.ADMIN_CANT_SELF
    if config.admin_level(uid):
        return texts.BAN_CANT_ADMIN
    return None


async def ban_user_view(session: AsyncSession, config: Config, actor_id: int, uid: int,
                        note: str = "") -> View | None:
    u = await _user(session, uid)
    if u is None:
        return None
    lines = [texts.BAN_TITLE, display_name(u)]
    back = [[cb("👤 Карточка", card(u.id)), cb("🔍 Другой пользователь", B("find"))],
            [cb("⬅️ Бан / разбан", B()), menu_btn()]]
    refusal = ban_refusal(config, actor_id, u.id)
    if u.is_banned:
        lines.append(f"🚫 <b>Забанен</b> {fmt_dt(u.banned_at)}" + (f": {html.escape(u.ban_reason)}" if u.ban_reason else "")
                     + (f" · обжалований: {u.appeals_count}" if u.appeals_count else ""))
        rows = [[cb("✅ Разбанить", B("unban", u.id))], *back]
    elif refusal:
        lines += ["", refusal]
        rows = back
    else:
        lines.append("\nВыбери причину бана — её увидит пользователь:")
        reasons = [cb(title, B("ask", u.id, code)) for code, title in users.BAN_REASONS.items()]
        rows = [reasons[:2], reasons[2:], [cb("✏️ Своя причина", B("custom", u.id)),
                                          cb("Без причины", B("ask", u.id, "none"))], *back]
    return (f"{note}\n\n" if note else "") + "\n".join(lines), ik(*rows)


def ban_confirm_view(u: User, reason: str | None, code: str) -> View:
    return (texts.BAN_CONFIRM.format(user=display_name(u), reason=html.escape(reason) if reason else "без причины"),
            ik([cb("🚫 Да, забанить", B("go", u.id, code))], [cb(texts.BTN_CANCEL, B("user", u.id))]))


def unban_confirm_view(u: User) -> View:
    reason = f"\nБыл забанен {fmt_dt(u.banned_at)}" + (f": {html.escape(u.ban_reason)}" if u.ban_reason else "")
    return (texts.UNBAN_CONFIRM.format(user=display_name(u), reason=reason),
            ik([cb("✅ Да, разбанить", B("unban_go", u.id))], [cb(texts.BTN_CANCEL, B("user", u.id))]))


async def do_unban(session: AsyncSession, bot: Bot, uid: int, admin_id: int) -> str:
    u = await _user(session, uid)
    if u is None:
        return texts.NOT_FOUND
    if not u.is_banned:
        return "ℹ️ Он не забанен."
    u.is_banned, u.ban_reason = False, None
    log_admin(session, admin_id, "unban", "user", u.id, via="manage")
    await session.commit()
    await safe_send(bot, u.id, texts.USER_UNBANNED, reply_markup=main_menu(False))
    return "✅ Разбанен"


# ------------------------------------------------------------------ 💸 Выдать / списать деньги
def give_hub_view() -> View:
    return texts.GIVE_USAGE, ik([cb("🔍 Найти пользователя", G("find"))], [hub_btn(), menu_btn()])


def _verb(amount: int) -> str:
    return "➕ Выдать" if amount > 0 else "➖ Списать"


def amount_prompt(u: User) -> tuple[str, InlineKeyboardMarkup]:
    return (texts.GIVE_AMOUNT_PROMPT.format(user=display_name(u), balance=fmt_rub(u.balance_rub)),
            ik([cancel_btn(card(u.id))]))


def _overflow() -> str:
    return texts.GIVE_OVERFLOW.format(max=fmt_rub(INT64_MAX))


def amount_problem(u: User, amount: int) -> str | None:
    """Сумма не влезает в 64 бита или списывать нечего — сразу говорим, без подтверждения."""
    if not admin_money.check_amount(amount, u.balance_rub):
        return _overflow()
    if amount < 0 and u.balance_rub <= 0:
        return texts.GIVE_NOTHING
    return None


def give_confirm_text(u: User, amount: int, comment: str, s: RuntimeSettings) -> str:
    after = u.balance_rub + amount if amount > 0 else max(0, u.balance_rub + amount)
    extra = ""
    if comment:
        extra = f"\nКомментарий: «{html.escape(comment)}»" + (" — клиент его увидит." if s.admin_give_show_comment
                                                               else " — только в журнале.")
    if amount < 0 and -amount > u.balance_rub:
        extra += texts.GIVE_CLAMP_WARN
    return texts.GIVE_CONFIRM.format(verb=_verb(amount), amount=fmt_rub(abs(amount)), user=display_name(u),
                                     before=fmt_rub(u.balance_rub), after=fmt_rub(after), comment=extra)


async def give_confirm(state: FSMContext, u: User, amount: int, comment: str, s: RuntimeSettings) -> View:
    """Экран «Подтвердить» — данные операции (с одноразовым кодом op) лежат в FSM, кнопка лишь подтверждает."""
    await state.set_state(None)
    await state.update_data(give={"uid": u.id, "amount": amount, "comment": comment, "op": admin_money.new_op()})
    return give_confirm_text(u, amount, comment, s), ik([cb(texts.GIVE_CONFIRM_BTN, G("ok", u.id, str(amount)))],
                                                       [cb(texts.BTN_CANCEL, G("cancel", u.id))])


async def start_give(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings, u: User,
                     amount: int, comment: str) -> None:
    """Команда или быстрое сообщение с суммой: сразу экран подтверждения (комментарий — из той же строки)."""
    await state.clear()
    problem = amount_problem(u, amount)
    if problem:
        await message.answer(problem, reply_markup=ik([cb("👤 Карточка", card(u.id)), cb("💸 Деньги", G())]))
        return
    text, kb = await give_confirm(state, u, amount, comment[:500], s)
    await message.answer(text, reply_markup=kb)


def result_kb(uid: int) -> InlineKeyboardMarkup:
    return ik([cb("💸 Ещё операция", G("u", uid)), cb("👤 Карточка", card(uid))], [hub_btn(), menu_btn()])


async def do_give(session: AsyncSession, bot: Bot, s: RuntimeSettings, data: dict, admin_id: int) -> str:
    uid, amount, comment = int(data["uid"]), int(data["amount"]), str(data.get("comment") or "")
    u = await _user(session, uid)
    if u is None:
        return texts.NOT_FOUND
    try:
        res = await admin_money.give(session, uid, amount, comment, admin_id, str(data["op"]), now_utc())
    except admin_money.GiveError as e:
        await session.rollback()
        return _overflow() if str(e) == "overflow" else f"⚠️ {html.escape(str(e))}"
    if res.duplicate:
        return texts.GIVE_DUPLICATE
    if res.nothing:
        return texts.GIVE_NOTHING
    note = texts.GIVE_DONE.format(verb="Выдали" if res.applied > 0 else "Списали", amount=fmt_rub(abs(res.applied)),
                                  user=display_name(u), balance=fmt_rub(res.after))
    if res.clamped:
        note += texts.GIVE_CLAMPED
    text = (texts.GIVE_USER_IN if res.applied > 0 else texts.GIVE_USER_OUT).format(amount=fmt_rub(abs(res.applied)))
    if comment and s.admin_give_show_comment:
        text += texts.GIVE_USER_COMMENT.format(comment=html.escape(comment))
    await safe_send(bot, uid, text)
    return note


# ------------------------------------------------------------------ команды 2 уровня (раньше вводов: из любого шага)
def _split(args: str | None) -> tuple[str, str]:
    who, _, rest = (args or "").strip().partition(" ")
    return who, rest.strip()


async def _find_or_say(message: Message, session: AsyncSession, query: str) -> User | None:
    u, error = await find_user(session, query)
    if u is None:
        await message.answer(error)
    return u


@l2.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                   s: RuntimeSettings) -> None:
    await state.clear()
    who, rest = _split(command.args)
    if not who:
        await _reply(message, give_hub_view())
        return
    u = await _find_or_say(message, session, who)
    if u is None:
        return
    if not rest:
        text, kb = amount_prompt(u)
        await ask_new(message, state, AdminSG.give_amount, text, kb, uid=u.id)
        return
    amount, comment = split_amount(rest)
    if amount is None:
        await message.answer(texts.GIVE_BAD_AMOUNT + "\n\n" + texts.GIVE_USAGE)
        return
    await start_give(message, state, session, s, u, amount, comment)


@l2.message(StateFilter(None), F.text.regexp(QUICK_RE), F.reply_to_message.is_(None))
async def quick_give(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings) -> None:
    """«@username +500» / «@username -500 комментарий» — то же, что /give, коротким сообщением."""
    m = QUICK_RE.match(message.text or "")
    amount, comment = split_amount(m.group(2)) if m else (None, "")
    if amount is None:
        await message.answer(texts.GIVE_BAD_AMOUNT)
        return
    u = await _find_or_say(message, session, "@" + m.group(1))
    if u is not None:
        await start_give(message, state, session, s, u, amount, comment)


@l2.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                  config: Config) -> None:
    await state.clear()
    who, reason = _split(command.args)
    if not who:
        await _reply(message, await ban_hub_view(session))
        return
    u = await _find_or_say(message, session, who)
    if u is None:
        return
    if reason and not u.is_banned and not ban_refusal(config, message.from_user.id, u.id):
        await state.update_data(ban={"uid": u.id, "reason": reason[:200]})
        await _reply(message, ban_confirm_view(u, reason[:200], "custom"))
        return
    await _reply(message, await ban_user_view(session, config, message.from_user.id, u.id))


@l2.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                    config: Config) -> None:
    await state.clear()
    who, _ = _split(command.args)
    if not who:
        await _reply(message, await ban_hub_view(session))
        return
    u = await _find_or_say(message, session, who)
    if u is None:
        return
    if u.is_banned:
        await _reply(message, unban_confirm_view(u))
    else:
        await _reply(message, await ban_user_view(session, config, message.from_user.id, u.id, "ℹ️ Он не забанен."))


@l2.message(Command("priority"))
async def cmd_priority(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    who, _ = _split(command.args)
    if not who:
        await _reply(message, vip_hub_view())
        return
    u = await _find_or_say(message, session, who)
    if u is not None:
        await _reply(message, await priority_confirm_view(session, u.id))


@l2.message(Command("freemonth"))
async def cmd_freemonth(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                        s: RuntimeSettings) -> None:
    await state.clear()
    who, rest = _split(command.args)
    if not who:
        await _reply(message, vip_hub_view())
        return
    u = await _find_or_say(message, session, who)
    if u is None:
        return
    parts = rest.split()
    if parts and parts[0].lower() in ("0", "off", "выкл", "снять", "нет"):
        await _reply(message, await fm_off_view(session, u.id))
        return
    if not parts:
        await _reply(message, await days_view(session, u.id, s))
        return
    days = _days(parts[0])
    if days is None:
        await message.answer(texts.VIP_NUMBER_BAD.format(max=MAX_DAYS) + " — дни. Пример: <code>/freemonth "
                             "@username 30 10</code>")
        return
    if len(parts) == 1:
        await _reply(message, await tasks_view(session, u.id, days, s))
        return
    pair = _pair(f"{days}.{parts[1]}")
    if pair is None:
        await message.answer(texts.VIP_NUMBER_BAD.format(max=MAX_TASKS) + " — задачи. Пример: <code>/freemonth "
                             "@username 30 10</code>")
        return
    await _reply(message, await fm_confirm_view(session, u.id, *pair))


@l2.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                       config: Config) -> None:
    await state.clear()
    who, rest = _split(command.args)
    if not who:
        await _reply(message, await staff.list_view(session, config, message.from_user.id))
        return
    u = await _find_or_say(message, session, who)
    if u is not None:
        level = 2 if rest.split()[:1] == ["2"] else 1
        await _reply(message, await staff.add_view(session, config, message.from_user.id, u, only_level=level))


@l2.message(Command("removeadmin"))
async def cmd_removeadmin(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                          config: Config) -> None:
    await state.clear()
    who, _ = _split(command.args)
    if not who:
        await _reply(message, await staff.list_view(session, config, message.from_user.id))
        return
    u = await _find_or_say(message, session, who)
    if u is not None:
        await _reply(message, await staff.remove_view(session, config, message.from_user.id, u.id))


# /db и /backup живут в dbq.py; регистрируем здесь, до вводов, — чтобы срабатывали из любого шага
l2.message(Command("db"))(dbq.cmd_db)
l2.message(Command("backup"))(dbq.cmd_backup)


# ------------------------------------------------------------------ поиск пользователя для экранов 2 уровня
BACK = {"vip": V(), "bn": B(), "gv": G(), "dbu": AdminCb(s="db")}


@l2.message(AdminSG.find_user)
async def find_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Config,
                     s: RuntimeSettings) -> None:
    data = await state.get_data()
    purpose = str(data.get("purpose") or "")
    u, error = await find_user(session, message.text)
    if u is None:  # ввод остаётся открытым — можно сразу прислать другой ник или ID
        await message.answer(error, reply_markup=ik([cancel_btn(BACK.get(purpose, AdminCb(s="g")))]))
        return
    await state.clear()
    await close_prompt(bot, data)
    if purpose == "gv":
        text, kb = amount_prompt(u)
        await ask_new(message, state, AdminSG.give_amount, text, kb, uid=u.id)
    elif purpose == "vip":
        await _reply(message, await vip_user_view(session, u.id))
    elif purpose == "bn":
        await _reply(message, await ban_user_view(session, config, message.from_user.id, u.id))
    elif purpose == "dbu":
        view = await dbq.user_report(session, u)
        log_admin(session, message.from_user.id, "db_report", "user", u.id, report="user")
        await session.commit()
        await _reply(message, view)
    else:
        await users.send_card(message, session, u.id, config)


async def _ask_find(call: CallbackQuery, state: FSMContext, purpose: str) -> None:
    await call.answer()
    await ask(call, state, AdminSG.find_user, f"{FIND_HEAD[purpose]}\n\n{texts.FIND_USER_PROMPT}",
              ik([cancel_btn(BACK[purpose])]), purpose=purpose)


# ------------------------------------------------------------------ вводы: сумма, комментарий, числа, причина
@l2.message(AdminSG.give_amount)
async def give_amount_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                            s: RuntimeSettings) -> None:
    data = await state.get_data()
    u = await _user(session, int(data.get("uid") or 0))
    if u is None:
        await state.clear()
        await message.answer(texts.NOT_FOUND)
        return
    amount, comment = split_amount(message.text or "")
    if amount is None:
        await message.answer(texts.GIVE_BAD_AMOUNT, reply_markup=ik([cancel_btn(card(u.id))]))
        return
    problem = amount_problem(u, amount)
    if problem == _overflow():
        await message.answer(problem, reply_markup=ik([cancel_btn(card(u.id))]))
        return
    await close_prompt(bot, data)
    if problem or comment:
        await start_give(message, state, session, s, u, amount, comment)
        return
    shown = " и в уведомление клиенту" if s.admin_give_show_comment else ""
    await ask_new(message, state, AdminSG.give_comment,
                  texts.GIVE_COMMENT_PROMPT.format(verb=_verb(amount), amount=fmt_rub(abs(amount)),
                                                   user=display_name(u), shown=shown),
                  ik([cb(texts.GIVE_NO_COMMENT_BTN, G("nc", u.id, str(amount)))], [cancel_btn(card(u.id))]),
                  uid=u.id, amount=amount)


@l2.message(AdminSG.give_comment)
async def give_comment_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                             s: RuntimeSettings) -> None:
    comment = (message.text or "").strip()
    data = await state.get_data()
    if not comment:
        await message.answer("Напиши комментарий текстом или нажми «Без комментария».")
        return
    u = await _user(session, int(data.get("uid") or 0))
    await close_prompt(bot, data)
    if u is None:
        await state.clear()
        await message.answer(texts.NOT_FOUND)
        return
    await start_give(message, state, session, s, u, int(data["amount"]), comment)


@l2.message(AdminSG.vip_number)
async def vip_number_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                           s: RuntimeSettings) -> None:
    data = await state.get_data()
    what = data.get("what")
    top = MAX_DAYS if what == "days" else MAX_TASKS
    t = (message.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= top:
        await message.answer(texts.VIP_NUMBER_BAD.format(max=top))
        return
    uid = int(data.get("uid") or 0)
    await state.clear()
    await close_prompt(bot, data)
    if what == "days":
        view = await tasks_view(session, uid, int(t), s)
    else:
        pair = _pair(f"{data.get('days')}.{t}")
        view = await fm_confirm_view(session, uid, *pair) if pair else None
    await _reply(message, view or texts.NOT_FOUND)


@l2.message(AdminSG.ban_custom)
async def ban_custom_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Напиши причину текстом.")
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    u = await _user(session, int(data.get("uid") or 0))
    if u is None:
        await message.answer(texts.NOT_FOUND)
        return
    await state.update_data(ban={"uid": u.id, "reason": reason[:200]})
    await _reply(message, ban_confirm_view(u, reason[:200], "custom"))


# ------------------------------------------------------------------ кнопки: хаб
@l2.callback_query(AdminCb.filter(F.s == "g"))
async def g_hub(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, *hub_view())


# ------------------------------------------------------------------ кнопки: ⭐ / 🎟
@l2.callback_query(_on("vip", ""))
async def vip_hub(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, *vip_hub_view())


@l2.callback_query(_on("vip", "find"))
async def vip_find(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_find(call, state, "vip")


async def _show(call: CallbackQuery, view: View | None) -> None:
    if view is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await show(call, *view)


@l2.callback_query(_on("vip", "user"))
async def vip_user(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _show(call, await vip_user_view(session, callback_data.id))


@l2.callback_query(_on("vip", "pr"))
@l2.callback_query(_on("u", "prio"))  # кнопка из старой карточки пользователя — теперь с подтверждением
async def vip_priority(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    await _show(call, await priority_confirm_view(session, callback_data.id))


@l2.callback_query(_on("vip", "pr_go"))
async def vip_priority_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    if callback_data.v not in ("on", "off"):
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    note = await set_priority(session, bot, callback_data.id, callback_data.v == "on", call.from_user.id)
    await _show(call, await vip_user_view(session, callback_data.id, note))


@l2.callback_query(_on("vip", "fm"))
async def vip_fm(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    await _show(call, await days_view(session, callback_data.id, s))


@l2.callback_query(_on("u", "fm"))  # старая карточка: «🎁 Дать месяц» / «🎁 Снять месяц»
async def legacy_fm(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    u = await _user(session, callback_data.id)
    if u is not None and free_month_active(u, now_utc()):
        await _show(call, await fm_off_view(session, u.id))
    else:
        await _show(call, await days_view(session, callback_data.id, s))


@l2.callback_query(_on("vip", "fd"))
async def vip_days(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    days = _days(callback_data.v)
    if days is None:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    await _show(call, await tasks_view(session, callback_data.id, days, s))


@l2.callback_query(_on("vip", "ft"))
async def vip_tasks(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    pair = _pair(callback_data.v)
    if pair is None:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    await _show(call, await fm_confirm_view(session, callback_data.id, *pair))


@l2.callback_query(_on("vip", "fdn", "ftn"))
async def vip_number(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    days = callback_data.v if callback_data.a == "ftn" else ""
    if callback_data.a == "ftn" and _days(days) is None:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    await call.answer()
    what, top = ("days", MAX_DAYS) if callback_data.a == "fdn" else ("tasks", MAX_TASKS)
    await ask(call, state, AdminSG.vip_number,
              texts.VIP_NUMBER_PROMPT.format(what="дней" if what == "days" else "задач", max=top),
              ik([cancel_btn(V("user", callback_data.id))]), uid=callback_data.id, what=what, days=days)


@l2.callback_query(_on("vip", "fm_go"))
async def vip_fm_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    pair = _pair(callback_data.v)
    if pair is None:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    note = await set_free_month(session, bot, callback_data.id, *pair, call.from_user.id)
    await _show(call, await vip_user_view(session, callback_data.id, note))


@l2.callback_query(_on("vip", "fmoff"))
async def vip_fm_off(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    await _show(call, await fm_off_view(session, callback_data.id))


@l2.callback_query(_on("vip", "fmoff_go"))
async def vip_fm_off_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    note = await clear_free_month(session, bot, callback_data.id, call.from_user.id)
    await _show(call, await vip_user_view(session, callback_data.id, note))


# ------------------------------------------------------------------ кнопки: 🚫 бан / разбан
@l2.callback_query(_on("bn", ""))
async def bn_hub(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    await show(call, *await ban_hub_view(session))


@l2.callback_query(_on("bn", "find"))
async def bn_find(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_find(call, state, "bn")


@l2.callback_query(_on("bn", "user"))
async def bn_user(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                  config: Config) -> None:
    await state.clear()
    await _show(call, await ban_user_view(session, config, call.from_user.id, callback_data.id))


@l2.callback_query(_on("bn", "ask"))
async def bn_ask(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config) -> None:
    u = await _user(session, callback_data.id)
    if u is None or (callback_data.v != "none" and callback_data.v not in users.BAN_REASONS):
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    refusal = ban_refusal(config, call.from_user.id, u.id)
    if refusal:
        await call.answer(refusal, show_alert=True)
        return
    await _show(call, ban_confirm_view(u, users.BAN_REASONS.get(callback_data.v), callback_data.v))


@l2.callback_query(_on("bn", "custom"))
async def bn_custom(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminSG.ban_custom, "✏️ Напиши причину бана — её увидит пользователь (до 200 символов):",
              ik([cancel_btn(B("user", callback_data.id))]), uid=callback_data.id)


@l2.callback_query(_on("bn", "go"))
async def bn_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession, bot: Bot,
                config: Config) -> None:
    uid, code = callback_data.id, callback_data.v
    refusal = ban_refusal(config, call.from_user.id, uid)
    if refusal:
        await call.answer(refusal, show_alert=True)
        return
    if code == "custom":
        pending = (await state.get_data()).get("ban") or {}
        if int(pending.get("uid") or 0) != uid:
            await call.answer(texts.STALE_BUTTON, show_alert=True)
            return
        reason = str(pending.get("reason") or "")[:200] or None
        await state.update_data(ban=None)
    elif code == "none" or code in users.BAN_REASONS:
        reason = users.BAN_REASONS.get(code)
    else:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    done = await users._do_ban(bot, session, state, uid, reason, call.from_user.id)  # noqa: SLF001
    note = ("🚫 Забанен" + (f": {html.escape(reason)}" if reason else "")) if done else "ℹ️ Уже забанен."
    await _show(call, await ban_user_view(session, config, call.from_user.id, uid, note))


@l2.callback_query(_on("bn", "unban"))
async def bn_unban(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config) -> None:
    u = await _user(session, callback_data.id)
    if u is not None and not u.is_banned:
        await _show(call, await ban_user_view(session, config, call.from_user.id, u.id, "ℹ️ Он не забанен."))
        return
    await _show(call, unban_confirm_view(u) if u else None)


@l2.callback_query(_on("bn", "unban_go"))
async def bn_unban_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                      config: Config) -> None:
    note = await do_unban(session, bot, callback_data.id, call.from_user.id)
    await _show(call, await ban_user_view(session, config, call.from_user.id, callback_data.id, note))


# ------------------------------------------------------------------ кнопки: 💸 деньги
@l2.callback_query(_on("gv", ""))
async def gv_hub(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, *give_hub_view())


@l2.callback_query(_on("gv", "find"))
async def gv_find(call: CallbackQuery, state: FSMContext) -> None:
    await _ask_find(call, state, "gv")


@l2.callback_query(_on("gv", "u"))
@l2.callback_query(AdminCb.filter((F.s == "u") & (F.a == "adj") & F.v.startswith("rub")))  # старая карточка: «➕ ₽»
async def gv_user(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    u = await _user(session, callback_data.id)
    if u is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    text, kb = amount_prompt(u)
    await ask(call, state, AdminSG.give_amount, text, kb, uid=u.id)


@l2.callback_query(_on("gv", "nc"))
async def gv_no_comment(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                        s: RuntimeSettings) -> None:
    data = await state.get_data()
    if await state.get_state() != AdminSG.give_comment.state or int(data.get("uid") or 0) != callback_data.id \
            or str(data.get("amount")) != callback_data.v:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    u = await _user(session, callback_data.id)
    if u is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await state.clear()
    problem = amount_problem(u, int(data["amount"]))
    if problem:
        await _show(call, (problem, result_kb(u.id)))
        return
    await _show(call, await give_confirm(state, u, int(data["amount"]), "", s))


@l2.callback_query(_on("gv", "ok", "big"))
async def gv_ok(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                s: RuntimeSettings, bot: Bot) -> None:
    data = (await state.get_data()).get("give") or {}
    if int(data.get("uid") or 0) != callback_data.id or str(data.get("amount")) != callback_data.v:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    amount, limit = int(data["amount"]), int(s.admin_give_confirm_over)
    if callback_data.a == "ok" and abs(amount) > limit:  # большая сумма — второе подтверждение («проверь нули»)
        u = await _user(session, callback_data.id)
        if u is None:
            await call.answer(texts.NOT_FOUND, show_alert=True)
            return
        big = fmt_rub(abs(amount))
        await _show(call, (texts.GIVE_BIG_CONFIRM.format(verb=_verb(amount), amount=big, user=display_name(u),
                                                         limit=fmt_rub(limit)),
                           ik([cb(texts.GIVE_BIG_BTN.format(amount=big), G("big", u.id, str(amount)))],
                              [cb(texts.BTN_CANCEL, G("cancel", u.id))])))
        return
    await state.update_data(give=None)
    note = await do_give(session, bot, s, data, call.from_user.id)
    await call.answer()
    await show(call, note, result_kb(callback_data.id))


@l2.callback_query(_on("gv", "cancel"))
async def gv_cancel(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                    config: Config) -> None:
    await state.clear()
    await call.answer("Отменено")
    view = await users.card_view(session, callback_data.id, config, config.admin_level(call.from_user.id))
    await show(call, *(view or give_hub_view()))


# ------------------------------------------------------------------ 1 уровень: /user и отказы
@l1.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                   config: Config) -> None:
    await state.clear()
    q = (command.args or "").strip()
    if not q:
        await ask_new(message, state, AdminSG.user_search, "🔍 Пришли id, @username или имя.",
                      ik([cancel_btn(AdminCb(s="u", a="list"))]))
        return
    u, error = await find_user(session, q)
    found = [u] if u is not None else await search_users(session, q)
    if len(found) == 1:
        await users.send_card(message, session, found[0].id, config)
    elif found:
        await message.answer(f"Нашли: {len(found)}", reply_markup=ik(
            *[[cb(users.button_label(x, config), card(x.id))] for x in found], [menu_btn()]))
    else:
        await message.answer(error)


@l1.message(Command(*L2_COMMAND_NAMES), IsLevel(1))
async def cmd_l2_denied(message: Message) -> None:
    await message.answer(texts.ADMIN_L2_ONLY)


@l1.message(StateFilter(None), F.text.regexp(QUICK_RE), F.reply_to_message.is_(None), IsLevel(1))
async def quick_give_denied(message: Message) -> None:
    await message.answer(texts.ADMIN_L2_ONLY)
