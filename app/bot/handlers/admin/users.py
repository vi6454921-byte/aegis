"""Админка → пользователи: список, поиск, карточка, сообщения (1 уровень); балансы, статусы, бан, Excel (2 уровень)."""
from __future__ import annotations

import html

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message, ReplyKeyboardRemove
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb, MenuCb
from app.bot.handlers.admin.base import L2, admin_router, ask, ask_new, cancel_btn, close_prompt, menu_btn
from app.bot.keyboards import cb, ik, main_menu, pager
from app.bot.notify import safe_send
from app.bot.render import order_line, unlimited_status
from app.bot.states import AdminSG
from app.bot.support import copyable, reply_to_ticket
from app.bot.ui import show
from app.config import Config
from app.db.models import Order, Ticket, Transaction, User
from app.services import channel as chsvc
from app.services import export, promo, roles, tickets
from app.services.admin_log import log_admin
from app.services.ledger import InsufficientFunds, active_lots
from app.services.payments import admin_adjust
from app.services.settings import RuntimeSettings
from app.services.users import display_name, free_month_active, referrals_of, search_users
from app.utils.money import fmt_rub, parse_rub
from app.utils.timeutil import fmt_date, fmt_dt, now_utc

router = admin_router("admin_users")
PER_PAGE = 10
LOT_SOURCES = {"referral": "за друга", "topup_bonus": "к пополнению", "refund": "возврат", "admin": "от админа",
               "channel": "за подписку", "ref_channel": "за друга (подписка)"}
SOURCES = {"direct": "прямой вход", "ref": "реф-ссылка"}
QUICK_AMOUNTS = (5000, 10000, 20000, 50000, 100000)
COMMENTS = {"n": "", "c": "Компенсация", "g": "Подарок", "e": "Ошибка оплаты"}
BAN_REASONS = {"spam": "Спам", "fraud": "Мошенничество", "abuse": "Оскорбления", "terms": "Нарушение условий"}
APPEAL_HINT = "Если считаешь, что это ошибка, отправь обращение: мы рассмотрим его и ответим здесь."


def U(a: str, user_id: int = 0, v: str = "", p: int = 0) -> AdminCb:  # noqa: N802
    return AdminCb(s="u", a=a, id=user_id, v=v, p=p)


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "u") & (F.a == action))


def icons(u: User, config: Config) -> str:
    out = ""
    if config.admin_level(u.id):
        out += "👮"
    if u.is_banned:
        out += "🚫"
    if u.is_priority:
        out += "⭐"
    if u.bot_blocked:
        out += "📵"
    return out + " " if out else ""


def button_label(u: User, config: Config) -> str:
    name = " ".join(p for p in (u.first_name, u.last_name) if p) or "без имени"
    return f"{icons(u, config)}{name}" + (f" · @{u.username}" if u.username else "") + f" · {fmt_date(u.created_at)}"


async def list_view(session: AsyncSession, page: int, config: Config, level: int) -> tuple[str, InlineKeyboardMarkup]:
    total = int(await session.scalar(select(func.count(User.id))) or 0)
    rows = (await session.execute(select(User).order_by(User.created_at.desc(), User.id.desc())
                                  .offset(page * PER_PAGE).limit(PER_PAGE))).scalars().all()
    buttons = [[cb(button_label(u, config), U("card", u.id))] for u in rows]
    top = [cb("🔍 Найти", U("search"))]
    if level >= 2:
        top.append(cb("📊 Excel-таблица", U("xlsx")))
    return (f"👥 <b>Пользователи</b>: {total}\nНовые сверху. 👮 админ · 🚫 бан · ⭐ приоритет · 📵 заблокировал бота",
            ik(top, *buttons, pager(page, total, PER_PAGE, lambda p: U("list", p=p)), [menu_btn()]))


def channel_line(u: User) -> str:
    if u.channel_status == chsvc.MEMBER:
        line = "✅ подписан"
    elif u.channel_status == chsvc.LEFT:
        line = "❌ не подписан"
    else:
        line = "не проверяли"
    if u.channel_bonus_at:
        line += f" · бонус +{fmt_rub(u.channel_bonus_amount)} {fmt_date(u.channel_bonus_at)}"
        if u.channel_bonus_revoked_at:
            line += f" (списан за отписку {fmt_date(u.channel_bonus_revoked_at)})"
    return line


async def card_view(session: AsyncSession, user_id: int, config: Config, level: int
                    ) -> tuple[str, InlineKeyboardMarkup] | None:
    u = await session.get(User, user_id, populate_existing=True)
    if u is None:
        return None
    now = now_utc()
    referrer = await session.get(User, u.referrer_id) if u.referrer_id else None
    n_orders = int(await session.scalar(select(func.count(Order.id)).where(
        Order.user_id == u.id, Order.status != "draft")) or 0)
    n_paid = int(await session.scalar(select(func.count(Order.id)).where(
        Order.user_id == u.id, Order.paid_at.is_not(None))) or 0)
    friends = await referrals_of(session, u.id)
    paid_friends = sum(1 for r, _ in friends if r.rewarded)
    sub_friends = sum(1 for r, _ in friends if r.sub_status == "rewarded")
    source = SOURCES.get(u.source, u.source) if u.source != "src" else f"метка src_{html.escape(u.source_tag or '')}"
    staff_level = config.admin_level(u.id)
    lines = [
        f"👤 <b>{display_name(u)}</b>",
        f"С нами с {fmt_dt(u.created_at)} · заходил {fmt_dt(u.last_seen_at)}",
        f"Источник: {source}" + (f" · пригласил {display_name(referrer)}" if referrer else ""),
        f"📱 Телефон: {html.escape(u.phone) if u.phone else 'не оставлял'}",
        "Условия: " + (f"приняты {fmt_dt(u.terms_accepted_at)}" if u.terms_accepted_at else "не приняты"),
        f"📢 Канал: {channel_line(u)}",
        f"\n💰 Рубли: <b>{fmt_rub(u.balance_rub)}</b> · 🎁 Бонусы: <b>{fmt_rub(u.balance_bonus)}</b>",
    ]
    if level >= 2:
        for lot in await active_lots(session, u.id, now):
            lines.append(f"• {fmt_rub(lot.remaining)} из {fmt_rub(lot.amount)}, до {fmt_date(lot.expires_at)} "
                         f"({LOT_SOURCES.get(lot.source, lot.source)})" + (" 🧪" if lot.is_test else ""))
    status = []
    if u.is_priority:
        status.append("⭐ Приоритет" + (" (за друзей)" if u.priority_auto else " (вручную)"))
    if free_month_active(u, now):
        status.append(f"🎁 Месяц бесплатно до {fmt_date(u.free_month_until)}, задач {u.free_month_tasks_left}")
    unl = unlimited_status(u, now)
    if unl:
        status.append(unl + (texts.UNLIMITED_SOURCE_CODE if u.unlimited_code_id
                             else texts.UNLIMITED_SOURCE_ADMIN if u.unlimited_by else ""))
    if u.bot_blocked:
        status.append("📵 Заблокировал бота")
    lines.append("Статус: " + (", ".join(status) or "обычный"))
    if u.is_banned:
        lines.append(f"🚫 <b>Забанен</b> {fmt_dt(u.banned_at)}" + (f": {html.escape(u.ban_reason)}" if u.ban_reason else "")
                     + (f" · обжалований: {u.appeals_count}" if u.appeals_count else ""))
    if staff_level:
        lines.append(f"👮 Админ: {roles.LEVELS[staff_level]}")
    lines.append(f"📦 Заказов: {n_orders}, оплачено: {n_paid} · 👥 Друзей: {len(friends)}, оплатили: {paid_friends}"
                 + (f", подписались: {sub_friends}" if sub_friends else ""))
    claim, posts = await promo.user_summary(session, u.id)
    if claim is not None or posts:
        task = texts.PROFILE_STATUS.get(claim.status, claim.status) if claim else "-"
        lines.append(f"🎯 Задания. Профиль: {task} · постов: {len(posts)}, начислено "
                     f"{fmt_rub(sum(p.credited for p in posts))}")
    n_tickets = int(await session.scalar(select(func.count(Ticket.id)).where(Ticket.user_id == u.id)) or 0)
    rows: list[list] = []
    if level >= 2:
        fm = free_month_active(u, now)
        rows += [  # рубли, ⭐ и 🎟 - экраны «🔐 Управление» (manage.py): с подтверждением и уведомлением
            [cb("💸 Выдать / списать ₽", AdminCb(s="gv", a="u", id=u.id))],
            [cb("➕ бонусы", U("adj", u.id, "bonus+")), cb("➖ бонусы", U("adj", u.id, "bonus-"))],
            [cb("⭐ Снять приоритет" if u.is_priority else "⭐ Дать приоритет", AdminCb(s="vip", a="pr", id=u.id)),
             cb("🎟 Снять месяц" if fm else "🎟 Дать месяц",
                AdminCb(s="vip", a="fmoff" if fm else "fm", id=u.id))],
            [cb(texts.USER_UNLIMITED_BTN.format(action=texts.USER_UNLIMITED_CHANGE if unl else texts.USER_UNLIMITED_GIVE),
                AdminCb(s="pc", a="user", id=u.id))],
        ]
        ban_row = [cb("✉️ Написать", U("msg", u.id))]
        if not staff_level:
            ban_row.insert(0, cb("✅ Разбанить", U("unban", u.id)) if u.is_banned else cb("🚫 Забанить", U("ban", u.id)))
        rows.append(ban_row)
        rows.append([cb("📦 Заказы", U("orders", u.id)), cb("📜 Операции", U("tx", u.id)),
                     cb("👥 Рефералы", U("refs", u.id))])
    else:
        rows.append([cb("✉️ Написать", U("msg", u.id)), cb("📦 Заказы", U("orders", u.id))])
    if n_tickets:
        rows.append([cb(f"📨 Обращения ({n_tickets})", U("tickets", u.id))])
    rows.append([cb("⬅️ К пользователям", U("list")), menu_btn()])
    return "\n".join(lines), ik(*rows)


async def send_card(message: Message, session: AsyncSession, user_id: int, config: Config, note: str = "") -> None:
    view = await card_view(session, user_id, config, config.admin_level(message.chat.id))
    if view is None:
        await message.answer(texts.NOT_FOUND)
        return
    await message.answer((f"{note}\n\n" if note else "") + view[0], reply_markup=view[1])


async def show_card(call: CallbackQuery, session: AsyncSession, user_id: int, config: Config, note: str = "") -> None:
    view = await card_view(session, user_id, config, config.admin_level(call.from_user.id))
    if view is None:
        await call.message.answer(texts.NOT_FOUND)
        return
    await show(call, (f"{note}\n\n" if note else "") + view[0], view[1])


# ------------------------------------------------------------------ список, поиск, карточка
@router.callback_query(_f("list"))
async def u_list(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    await state.clear()
    await call.answer()
    text, kb = await list_view(session, max(callback_data.p, 0), config, config.admin_level(call.from_user.id))
    await show(call, text, kb)


@router.callback_query(_f("search"))
async def u_search(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminSG.user_search, "🔍 Пришли id, @username или имя.", ik([cancel_btn(U("list"))]))


@router.message(AdminSG.user_search)
async def u_search_input(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                         config: Config) -> None:
    found = await search_users(session, message.text or "")
    if not found:
        await message.answer("Никого не нашли. Попробуй ещё раз или нажми «Отмена».",
                             reply_markup=ik([cancel_btn(U("list"))]))
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    if len(found) == 1:
        await send_card(message, session, found[0].id, config)
        return
    await message.answer(f"Нашли: {len(found)}", reply_markup=ik(*[[cb(button_label(u, config), U("card", u.id))]
                                                                  for u in found], [menu_btn()]))


@router.callback_query(_f("card"))
async def u_card(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    await state.clear()
    await call.answer()
    await show_card(call, session, callback_data.id, config)


@router.callback_query(_f("open"))
async def u_open(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    await state.clear()
    await call.answer()
    view = await card_view(session, callback_data.id, config, config.admin_level(call.from_user.id))
    if view is None:
        await call.message.answer(texts.NOT_FOUND)
        return
    await call.message.answer(view[0], reply_markup=view[1])


@router.callback_query(_f("xlsx"), L2)
async def u_xlsx(call: CallbackQuery, session: AsyncSession, config: Config) -> None:
    await call.answer("Готовим таблицу…")
    path = await export.export_users_xlsx(session, config.exports_dir)
    log_admin(session, call.from_user.id, "export_users_xlsx", None, None, file=path.name)  # персональные данные
    await session.commit()
    await call.message.answer_document(FSInputFile(path, filename=path.name),
                                       caption="📊 Пользователи, покупки и пополнения: Excel-таблица.\n"
                                               "<i>Здесь персональные данные (телефоны): не пересылай файл "
                                               "посторонним.</i>")


# ------------------------------------------------------------------ начислить / списать (2 уровень)
def _adj_words(account: str, sign: str) -> tuple[str, str]:
    verb = "➕ Начислить" if sign == "+" else "➖ Списать"
    what = "рубли" if account == "rub" else "бонусы"
    return verb, what


@router.callback_query(_f("adj"), L2)
async def u_adj(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    account, sign = callback_data.v[:-1], callback_data.v[-1:]
    u = await session.get(User, callback_data.id)
    if account not in ("rub", "bonus") or sign not in ("+", "-") or u is None:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    verb, what = _adj_words(account, sign)
    now_bal = u.balance_rub if account == "rub" else u.balance_bonus
    amounts = [cb(fmt_rub(a), U("adj_amt", u.id, f"{account}{sign}{a}")) for a in QUICK_AMOUNTS]
    await ask(call, state, AdminSG.adjust_amount,
              f"{verb} <b>{what}</b>: {display_name(u)}\nСейчас: {fmt_rub(now_bal)}\n\n"
              "Выбери сумму или напиши свою (например, <code>75</code> или <code>99,90</code>):",
              ik(amounts[:3], amounts[3:], [cancel_btn(U("card", u.id))]), uid=u.id, account=account, sign=sign)


async def _comment_step(target: CallbackQuery | Message, state: FSMContext, session: AsyncSession,
                        amount: int) -> None:
    data = await state.get_data()
    uid, account, sign = int(data["uid"]), data["account"], data["sign"]
    u = await session.get(User, uid)
    verb, what = _adj_words(account, sign)
    text = (f"{verb} <b>{fmt_rub(amount)}</b> ({what}): {display_name(u)}\n\n"
            "Комментарий: его увидит клиент и он попадёт в журнал. Выбери или напиши свой:")
    kb = ik([cb("Без комментария", U("adj_go", uid, "n")), cb(COMMENTS["c"], U("adj_go", uid, "c"))],
            [cb(COMMENTS["g"], U("adj_go", uid, "g")), cb(COMMENTS["e"], U("adj_go", uid, "e"))],
            [cancel_btn(U("card", uid))])
    if isinstance(target, CallbackQuery):
        await ask(target, state, AdminSG.adjust_comment, text, kb, amount=amount)
    else:
        await ask_new(target, state, AdminSG.adjust_comment, text, kb, amount=amount)


@router.callback_query(_f("adj_amt"), L2)
async def u_adj_quick(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    v = callback_data.v
    account = "rub" if v.startswith("rub") else "bonus"
    sign = v[len(account):len(account) + 1]
    if await state.get_state() != AdminSG.adjust_amount.state or int(data.get("uid") or 0) != callback_data.id \
            or data.get("account") != account or data.get("sign") != sign or not v[len(account) + 1:].isdigit():
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    await call.answer()
    await _comment_step(call, state, session, int(v[len(account) + 1:]))


@router.message(AdminSG.adjust_amount)
async def u_adj_amount(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    amount = parse_rub(message.text or "")
    if amount is None:
        await message.answer("Нужна сумма больше нуля, например 50: или выбери кнопкой выше.")
        return
    await close_prompt(bot, await state.get_data())
    await _comment_step(message, state, session, amount)


async def _do_adjust(session: AsyncSession, s: RuntimeSettings, bot: Bot, data: dict, comment: str,
                     admin_id: int) -> str:
    amount, account, uid = int(data["amount"]), data["account"], int(data["uid"])
    signed = amount if data["sign"] == "+" else -amount
    try:
        await admin_adjust(session, uid, account, signed, comment[:500], s, now_utc(), admin_id)
    except InsufficientFunds:
        await session.rollback()
        return "❌ Недостаточно средств для списания: баланс не меняли."
    tail = f": {html.escape(comment)}" if comment else "."
    if signed > 0:
        what = "на рублёвый баланс" if account == "rub" else "бонусами"
        await safe_send(bot, uid, f"💰 Тебе начислено {fmt_rub(amount)} {what}{tail}")
    else:
        what = "с рублёвого баланса" if account == "rub" else "бонусов"
        await safe_send(bot, uid, f"Списано {fmt_rub(amount)} {what}{tail}")
    return f"✅ {'Начислено' if signed > 0 else 'Списано'} {fmt_rub(amount)}" + (f" · «{html.escape(comment)}»"
                                                                                  if comment else "")


@router.callback_query(_f("adj_go"), L2)
async def u_adj_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                   s: RuntimeSettings, bot: Bot, config: Config) -> None:
    data = await state.get_data()
    if await state.get_state() != AdminSG.adjust_comment.state or int(data.get("uid") or 0) != callback_data.id:
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    note = await _do_adjust(session, s, bot, data, COMMENTS.get(callback_data.v, ""), call.from_user.id)
    await call.answer(note[:200])
    await show_card(call, session, callback_data.id, config, note)


@router.message(AdminSG.adjust_comment)
async def u_adj_comment(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings,
                        bot: Bot, config: Config) -> None:
    comment = (message.text or "").strip()
    if not comment:
        await message.answer("Напиши комментарий текстом или выбери кнопкой выше.")
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    note = await _do_adjust(session, s, bot, data, comment, message.from_user.id)
    await send_card(message, session, int(data["uid"]), config, note)


# ------------------------------------------------------------------ бан (2 уровень)
@router.callback_query(_f("ban"), L2)
async def u_ban(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, config: Config) -> None:
    u = await session.get(User, callback_data.id, populate_existing=True)
    if u is None:
        await call.answer(texts.NOT_FOUND)
        return
    if config.admin_level(u.id):
        await call.answer("Админа забанить нельзя: сначала сними доступ в «👮 Админы».", show_alert=True)
        return
    if u.is_banned:
        await call.answer("Уже забанен.")
        await show_card(call, session, u.id, config)
        return
    await call.answer()
    reasons = [cb(title, U("ban_go", u.id, code)) for code, title in BAN_REASONS.items()]
    await show(call, f"🚫 <b>Забанить</b> {display_name(u)}?\n\nБот для него закроется, но он сможет отправить "
                     "обжалование: оно придёт в «📨 Обращения». Выбери причину (её увидит пользователь):",
               ik(reasons[:2], reasons[2:], [cb("✏️ Своя причина", U("ban_custom", u.id)),
                                             cb("Без причины", U("ban_go", u.id, "none"))],
                  [cb(texts.BTN_CANCEL, U("card", u.id))]))


async def _do_ban(bot: Bot, session: AsyncSession, state: FSMContext, uid: int, reason: str | None,
                  admin_id: int) -> bool:
    u = await session.get(User, uid, populate_existing=True)
    if u is None or u.is_banned:
        return False
    u.is_banned, u.ban_reason, u.banned_at = True, reason, now_utc()
    log_admin(session, admin_id, "ban", "user", u.id, reason=reason)
    await session.commit()
    other = FSMContext(storage=state.storage, key=StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid))
    await other.clear()
    reason_txt = f": {html.escape(reason)}" if reason else ""
    await safe_send(bot, uid, texts.USER_BANNED_NOTICE.format(reason=reason_txt), reply_markup=ReplyKeyboardRemove())
    await safe_send(bot, uid, APPEAL_HINT, reply_markup=ik([cb(texts.APPEAL_BTN, MenuCb(action="appeal"))]))
    return True


@router.callback_query(_f("ban_go"), L2)
async def u_ban_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                   bot: Bot, config: Config) -> None:
    if config.admin_level(callback_data.id):
        await call.answer("Админа забанить нельзя.", show_alert=True)
        return
    reason = BAN_REASONS.get(callback_data.v)
    done = await _do_ban(bot, session, state, callback_data.id, reason, call.from_user.id)
    await call.answer("Забанен 🚫" if done else "Уже забанен")
    await show_card(call, session, callback_data.id, config, "🚫 Забанен" + (f": {reason}" if reason else ""))


@router.callback_query(_f("ban_custom"), L2)
async def u_ban_custom(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminSG.ban_reason, "✏️ Напиши причину бана: её увидит пользователь (до 200 символов):",
              ik([cancel_btn(U("card", callback_data.id))]), uid=callback_data.id)


@router.message(AdminSG.ban_reason)
async def u_ban_reason(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Config) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Напиши причину текстом.")
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    uid = int(data.get("uid") or 0)
    if config.admin_level(uid):
        await message.answer("Админа забанить нельзя.")
        return
    await _do_ban(bot, session, state, uid, reason[:200], message.from_user.id)
    await send_card(message, session, uid, config, f"🚫 Забанен: {html.escape(reason[:200])}")


@router.callback_query(_f("unban"), L2)
async def u_unban(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                  config: Config) -> None:
    u = await session.get(User, callback_data.id, populate_existing=True)
    if u is None:
        await call.answer(texts.NOT_FOUND)
        return
    if u.is_banned:
        u.is_banned, u.ban_reason = False, None
        log_admin(session, call.from_user.id, "unban", "user", u.id)
        await session.commit()
        await safe_send(bot, u.id, texts.USER_UNBANNED, reply_markup=main_menu(False))
    await call.answer("Разбанен ✅")
    await show_card(call, session, u.id, config, "✅ Разбанен")


# ------------------------------------------------------------------ сообщение от имени бота (1 уровень)
@router.callback_query(_f("msg"))
async def u_msg(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    u = await session.get(User, callback_data.id)
    await call.answer()
    await ask(call, state, AdminSG.message_user,
              f"✉️ <b>Сообщение для</b> {display_name(u)}\n"
              "Пришли текст, фото или файл: клиент получит его от имени бота и сможет ответить.",
              ik([cancel_btn(U("card", callback_data.id))]), uid=callback_data.id)


@router.message(AdminSG.message_user)
async def u_msg_send(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Config) -> None:
    if not copyable(message):
        await message.answer("Такое сообщение переслать нельзя: пришли текст, фото или файл.")
        return
    data = await state.get_data()
    uid = int(data.get("uid") or 0)
    await state.clear()
    await close_prompt(bot, data)
    ticket, _ = await tickets.open_or_append(session, uid, "support", now_utc())
    admin = await session.get(User, message.from_user.id)
    ok = await reply_to_ticket(bot, session, config, ticket, admin, message.from_user.id, message,
                               header="💬 <b>Сообщение от команды Аегис</b>")
    log_admin(session, message.from_user.id, "message_user", "user", uid, delivered=ok)
    await session.commit()
    await send_card(message, session, uid, config,
                    "✅ Доставлено" if ok else "❌ Не доставлено: пользователь заблокировал бота")


# ------------------------------------------------------------------ заказы, операции, рефералы, обращения
@router.callback_query(_f("orders"))
async def u_orders(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    rows = (await session.execute(select(Order).where(Order.user_id == callback_data.id, Order.status != "draft")
                                  .order_by(Order.id.desc()).limit(20))).scalars().all()
    await call.answer()
    lines = [f"📦 <b>Заказы</b> id <code>{callback_data.id}</code>: {len(rows)}"] + [order_line(o) for o in rows]
    buttons = [[cb(f"#{o.id} · {texts.STATUS.get(o.status, o.status)}", AdminCb(s="o", a="card", id=o.id))]
               for o in rows]
    await show(call, "\n".join(lines) if rows else "Заказов нет.",
               ik(*buttons, [cb("⬅️ Карточка", U("card", callback_data.id))]))


@router.callback_query(_f("tickets"))
async def u_tickets(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    rows = (await session.execute(select(Ticket).where(Ticket.user_id == callback_data.id)
                                  .order_by(Ticket.id.desc()).limit(15))).scalars().all()
    await call.answer()
    buttons = [[cb(f"№{t.id} · {tickets.title(t)} · {tickets.STATUS.get(t.status, t.status)}",
                   AdminCb(s="t", a="open", id=t.id))] for t in rows]
    await show(call, f"📨 <b>Обращения</b> id <code>{callback_data.id}</code>: {len(rows)}",
               ik(*buttons, [cb("⬅️ Карточка", U("card", callback_data.id))]))


@router.callback_query(_f("tx"), L2)
async def u_tx(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    rows = (await session.execute(select(Transaction).where(Transaction.user_id == callback_data.id)
                                  .order_by(Transaction.id.desc()).limit(25))).scalars().all()
    await call.answer()
    lines = [f"📜 <b>Операции</b> id <code>{callback_data.id}</code> (последние 25)"]
    for tx in rows:
        acc = "₽" if tx.account == "rub" else "бон."
        sign = "+" if tx.amount > 0 else ""
        what = texts.TX_KINDS.get(tx.kind, tx.kind) + (f" #{tx.order_id}" if tx.order_id else "")
        extra = (f" · {html.escape(tx.comment)}" if tx.comment else "") + (" 🧪" if tx.is_test else "")
        lines.append(f"{fmt_dt(tx.created_at)} {sign}{fmt_rub(tx.amount)} {acc} · {what}{extra}")
    if not rows:
        lines.append("Операций нет.")
    await show(call, "\n".join(lines)[:4000], ik([cb("⬅️ Карточка", U("card", callback_data.id))]))


SUB_STATUS = {"none": "не подписался", "pending": "⏳ подписался, ждём срок удержания",
              "rewarded": "✅ подписка засчитана", "cancelled": "✖️ отписался: не засчитан"}


@router.callback_query(_f("refs"), L2)
async def u_refs(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    friends = await referrals_of(session, callback_data.id)
    await call.answer()
    lines = [f"👥 <b>Приглашённые</b> id <code>{callback_data.id}</code>: {len(friends)}"]
    for ref, user in friends[:40]:
        state = f"✅ оплатил (заказ #{ref.reward_order_id}, +{fmt_rub(ref.reward_amount)})" if ref.rewarded \
            else "без оплаченного заказа"
        if ref.revoked_count:
            state += f" · отзывов: {ref.revoked_count}"
        sub = SUB_STATUS.get(ref.sub_status, ref.sub_status)
        if ref.sub_status == "rewarded":
            sub += f" (+{fmt_rub(ref.sub_reward_amount)})"
        lines.append(f"• {display_name(user)} · {state} · канал: {sub}")
    await show(call, "\n".join(lines)[:4000], ik([cb("⬅️ Карточка", U("card", callback_data.id))]))
