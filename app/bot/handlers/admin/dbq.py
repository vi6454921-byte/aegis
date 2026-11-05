"""Админка → 🗄 Запросы к БД (2 уровень): файл базы, выгрузки и готовые отчёты — только чтение.

Произвольного SQL нет: только заранее заданные запросы через ORM, ничего не меняют. Суммы считаем в Python
(целые без предела), а не SUM() в SQLite — огромные суммы переполнили бы 64-битное целое прямо в запросе.
Каждый отчёт, файл и выгрузка записываются в журнал админов (admin_log).
"""
from __future__ import annotations

import html
from collections import defaultdict

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.base import admin_router, ask, cancel_btn, menu_btn
from app.bot.handlers.admin.data import send_backup
from app.bot.keyboards import cb, ik
from app.bot.render import order_line
from app.bot.states import AdminSG
from app.bot.ui import show
from app.config import Config
from app.db.models import Order, Payment, ProfileClaim, PromoPost, PromoPostReport, Referral, Transaction, User
from app.services import orders as osvc
from app.services import promo, tickets
from app.services.admin_log import log_admin
from app.services.users import display_name, short_name
from app.utils.money import fmt_rub
from app.utils.timeutil import fmt_dt

router = admin_router("admin_dbq", level=2)
View = tuple[str, InlineKeyboardMarkup]
RECENT = 20
PAY_STATUS = {"pending": "⏳ ждёт", "paid": "✅ оплачен", "refunded": "↩️ возврат", "cancelled": "✖️ отменён"}
PROVIDERS = {"test": "🧪 тест", "stars": "⭐ звёзды", "manual": "🏦 перевод", "internal": "💰 с баланса"}
CLAIM_STATUS = {"pending": "⏳ на проверке", "approved": "✅ начислено", "rejected": "❌ отклонено",
                "revoked": "↩️ списано (ссылку убрали)"}
ADMIN_KINDS = ("admin_topup", "admin_withdraw", "admin_credit", "admin_debit")


def D(a: str = "", uid: int = 0) -> AdminCb:  # noqa: N802
    return AdminCb(s="db", a=a, id=uid)


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "db") & (F.a == action))


def _back() -> list:
    return [cb("⬅️ Запросы к БД", D()), menu_btn()]


def hub_view() -> View:
    return (f"{texts.DBQ_TITLE}\n{texts.DBQ_INTRO}",
            ik([cb("💾 Файл базы", D("file"))],
               [cb("📤 Экспорт CSV", AdminCb(s="d", a="csv", v="db")), cb("📊 Excel: пользователи", AdminCb(s="u", a="xlsx", v="db"))],
               [cb("👤 Пользователь", D("find"))],
               [cb("📦 Последние заказы", D("orders")), cb("💳 Платежи и начисления", D("money"))],
               [cb("📋 На модерации", D("mod"))],
               [cb("⬅️ Управление", AdminCb(s="g")), menu_btn()]))


def _sum(values) -> int:  # noqa: ANN001
    return sum(int(v or 0) for v in values)


# ------------------------------------------------------------------ отчёты
async def user_report(session: AsyncSession, u: User) -> View:
    """Пользователь: балансы, заказы, рефералы, награды за задания, деньги от админов."""
    txs = list((await session.execute(select(Transaction).where(Transaction.user_id == u.id))).scalars())
    by_kind: dict[tuple[str, str], int] = defaultdict(int)
    for tx in txs:
        by_kind[(tx.account, tx.kind)] += int(tx.amount)
    orders = list((await session.execute(select(Order).where(Order.user_id == u.id, Order.status != "draft")
                                         .order_by(Order.id.desc()))).scalars())
    paid = [o for o in orders if o.paid_at is not None]
    refs = list((await session.execute(select(Referral).where(Referral.referrer_id == u.id))).scalars())
    claim, posts = await promo.user_summary(session, u.id)
    lines = ["👤 <b>Отчёт по пользователю</b>", display_name(u),
             f"С нами с {fmt_dt(u.created_at)} · заходил {fmt_dt(u.last_seen_at)}"
             + (" · 🚫 забанен" if u.is_banned else ""),
             "", "<b>Балансы</b>",
             f"💰 Рубли: <b>{fmt_rub(u.balance_rub)}</b> · 🎁 Бонусы: <b>{fmt_rub(u.balance_bonus)}</b>",
             f"Журнал: рубли {fmt_rub(_sum(t.amount for t in txs if t.account == 'rub'))}, "
             f"бонусы {fmt_rub(_sum(t.amount for t in txs if t.account == 'bonus'))} · операций: {len(txs)}",
             "", "<b>Заказы</b>",
             f"📦 Всего: {len(orders)}, оплачено: {len(paid)} · рублями {fmt_rub(_sum(o.rub_paid for o in paid))}, "
             f"бонусами {fmt_rub(_sum(o.paid_bonus for o in paid))}"]
    lines += [order_line(o) for o in orders[:5]]
    lines += ["", "<b>Рефералы</b>",
              f"👥 Пригласил: {len(refs)}, оплатили: {sum(1 for r in refs if r.rewarded)} · бонусов за друзей "
              f"{fmt_rub(_sum(r.reward_amount for r in refs if r.rewarded))}, за подписки "
              f"{fmt_rub(_sum(r.sub_reward_amount for r in refs if r.sub_status == 'rewarded'))}"]
    task_total = _sum(v for (acc, kind), v in by_kind.items() if acc == "rub" and kind in promo.TASK_KINDS)
    lines += ["", "<b>Задания</b>",
              "📌 Ссылка в профиле: " + (CLAIM_STATUS.get(claim.status, claim.status) if claim else "не отправлял"),
              f"📣 Постов: {len(posts)}, подтверждено просмотров: {sum(p.verified_views for p in posts)}, "
              f"начислено {fmt_rub(_sum(p.credited for p in posts))}",
              f"Итого за задания по журналу: {fmt_rub(task_total)}"]
    admin_in = _sum(v for (acc, kind), v in by_kind.items() if acc == "rub" and kind in ADMIN_KINDS and v > 0)
    admin_out = _sum(v for (acc, kind), v in by_kind.items() if acc == "rub" and kind in ADMIN_KINDS and v < 0)
    if admin_in or admin_out:
        lines.append(f"\n💸 От админов: выдано {fmt_rub(admin_in)}, списано {fmt_rub(-admin_out)}")
    kb = ik([cb("👤 Карточка", AdminCb(s="u", a="card", id=u.id)), cb("📜 Операции", AdminCb(s="u", a="tx", id=u.id))],
            [cb("🔍 Другой пользователь", D("find"))], _back())
    return "\n".join(lines)[:4000], kb


async def orders_report(session: AsyncSession) -> View:
    rows = list((await session.execute(select(Order, User).join(User, User.id == Order.user_id)
                                       .where(Order.status != "draft")
                                       .order_by(Order.id.desc()).limit(RECENT))).all())
    lines = [f"📦 <b>Последние заказы</b> ({len(rows)})"]
    for o, u in rows:
        price = f" · {fmt_rub(o.price)}" if o.price is not None else ""
        lines.append(f"{order_line(o)} · {short_name(u)}{price}" + (" 🧪" if o.is_test else ""))
    if not rows:
        lines.append("Заказов пока нет.")
    return "\n".join(lines)[:4000], ik([cb("🔄 Обновить", D("orders"))], _back())


async def money_report(session: AsyncSession) -> View:
    pays = list((await session.execute(select(Payment, User).join(User, User.id == Payment.user_id)
                                       .order_by(Payment.id.desc()).limit(15))).all())
    credits = list((await session.execute(select(Transaction, User).join(User, User.id == Transaction.user_id)
                                          .where(Transaction.amount > 0)
                                          .order_by(Transaction.id.desc()).limit(15))).all())
    lines = ["💳 <b>Последние платежи</b>"]
    for p, u in pays:
        what = f"заказ #{p.order_id}" if p.order_id else "пополнение"
        lines.append(f"№{p.id} · {fmt_dt(p.created_at)} · {PAY_STATUS.get(p.status, p.status)} · "
                     f"{PROVIDERS.get(p.provider, p.provider)} · {fmt_rub(p.amount_rub)} · {what} · {short_name(u)}"
                     + (" 🧪" if p.is_test else ""))
    if not pays:
        lines.append("Платежей пока нет.")
    lines += ["", "💰 <b>Последние начисления</b>"]
    for tx, u in credits:
        acc = "₽" if tx.account == "rub" else "бон."
        extra = f" · {html.escape(tx.comment[:60])}" if tx.comment else ""
        lines.append(f"{fmt_dt(tx.created_at)} +{fmt_rub(tx.amount)} {acc} · {texts.TX_KINDS.get(tx.kind, tx.kind)} · "
                     f"{short_name(u)}{extra}" + (" 🧪" if tx.is_test else ""))
    if not credits:
        lines.append("Начислений пока нет.")
    return "\n".join(lines)[:4000], ik([cb("🔄 Обновить", D("money"))], _back())


async def moderation_report(session: AsyncSession) -> View:
    c = await osvc.status_counts(session)
    receipts = int(await session.scalar(select(func.count(Payment.id)).where(
        Payment.provider == "manual", Payment.claimed_at.is_not(None), Payment.reviewed_at.is_(None),
        Payment.status.in_(("pending", "cancelled")))) or 0)
    n_tickets = await tickets.open_count(session)
    tasks = await promo.pending_counts(session)
    oldest_claim = await session.scalar(select(func.min(ProfileClaim.created_at)).where(ProfileClaim.status == "pending"))
    oldest_report = await session.scalar(select(func.min(PromoPostReport.created_at))
                                         .where(PromoPostReport.status == "pending"))
    active_posts = int(await session.scalar(select(func.count(PromoPost.id))
                                            .where(PromoPost.status.in_(promo.POST_ACTIVE))) or 0)
    lines = ["📋 <b>На модерации</b>",
             f"🔎 Заказы на проверке (выставить счёт): <b>{c.get('review', 0)}</b>",
             f"⚠️ Споры «не зачли»: <b>{c.get('dispute', 0)}</b>",
             f"🧾 Чеки об оплате переводом: <b>{receipts}</b>",
             f"📨 Обращения без ответа: <b>{n_tickets}</b>",
             f"📌 Ссылка в профиле: <b>{tasks['profile']}</b>"
             + (f" · старейшая с {fmt_dt(oldest_claim)}" if oldest_claim else ""),
             f"📣 Посты: <b>{tasks['post']}</b> · 📈 отчёты о просмотрах: <b>{tasks['views']}</b>"
             + (f" · старейший с {fmt_dt(oldest_report)}" if oldest_report else ""),
             f"Активных постов (на проверке и принятых): {active_posts}"]
    return "\n".join(lines), ik([cb("📥 Заказы", AdminCb(s="o", a="list", v="q")), cb("🧾 Чеки", AdminCb(s="pay"))],
                                [cb("🎯 Задания", AdminCb(s="pt")), cb("📨 Обращения", AdminCb(s="t"))],
                                [cb("🔄 Обновить", D("mod"))], _back())


REPORTS = {"orders": orders_report, "money": money_report, "mod": moderation_report}


# ------------------------------------------------------------------ хендлеры
async def cmd_db(message: Message, state: FSMContext) -> None:
    """/db — регистрируется в manage.py (раньше вводов, из любого шага)."""
    await state.clear()
    text, kb = hub_view()
    await message.answer(text, reply_markup=kb)


async def cmd_backup(message: Message, state: FSMContext, session: AsyncSession, config: Config, bot: Bot) -> None:
    await state.clear()
    await message.answer("💾 Делаю бэкап…")
    await send_backup(bot, message.chat.id, session, config, message.from_user.id)


@router.callback_query(_f(""))
async def db_hub(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, *hub_view())


@router.callback_query(_f("file"))
async def db_file(call: CallbackQuery, session: AsyncSession, config: Config, bot: Bot) -> None:
    await call.answer("Делаю бэкап…")
    await send_backup(bot, call.from_user.id, session, config, call.from_user.id)


@router.callback_query(_f("find"))
async def db_find(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await ask(call, state, AdminSG.find_user, f"👤 <b>Отчёт по пользователю</b>\n\n{texts.FIND_USER_PROMPT}",
              ik([cancel_btn(D())]), purpose="dbu")


@router.callback_query(_f("user"))
async def db_user(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    u = await session.get(User, callback_data.id, populate_existing=True)
    if u is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    view = await user_report(session, u)
    log_admin(session, call.from_user.id, "db_report", "user", u.id, report="user")
    await session.commit()
    await show(call, *view)


@router.callback_query(AdminCb.filter((F.s == "db") & F.a.in_(set(REPORTS))))
async def db_report(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    await call.answer()
    view = await REPORTS[callback_data.a](session)
    log_admin(session, call.from_user.id, "db_report", None, None, report=callback_data.a)
    await session.commit()
    await show(call, *view)
