"""Оформление заказа по шагам: «📝 Заказать» (разбор задач) и «📸 Объяснить материал».

Черновик — строка orders (status=draft) с полем wizard_step, состояние диалога — FSM в БД.
После рестарта бота пользователь продолжает с того же шага. Над каждым шагом — прогресс «Шаг 2 из 5 ▰▰▱▱▱»,
на кнопках срока — наценка, которая получится (из настроек админки).
"""
from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot import drafts
from app.bot import files as upl
from app.bot import guide
from app.bot.callbacks import MenuCb, WizCb
from app.bot.handlers.common import menu_router
from app.bot.keyboards import cb, ik, main_menu, order_alert_kb
from app.bot.notify import notify_admins, remember_all, send_files, staff_targets
from app.bot.render import subject_name, tasks_word
from app.bot.states import OrderSG
from app.bot.ui import drop_kb, show
from app.config import Config
from app.db.models import Order, OrderFile, User
from app.services import orders as svc
from app.services import slots
from app.services.ledger import available_bonus
from app.services.pricing import hours_between
from app.services.settings import RuntimeSettings
from app.services.users import display_name, free_month_active
from app.utils.money import fmt_mult, fmt_rub
from app.utils.timeutil import WEEKDAYS, end_of_local_day, fmt_dt, fmt_left, local_today, now_utc, parse_deadline

log = logging.getLogger(__name__)
router = Router(name="order")
M = texts.MAIN_MENU

STATES: dict[str, State] = {
    "subject": OrderSG.subject, "photos": OrderSG.photos, "count": OrderSG.count, "variant": OrderSG.variant,
    "deadline": OrderSG.deadline, "deadline_pick": OrderSG.deadline_pick, "lecture": OrderSG.lecture,
    "practice": OrderSG.practice, "comment": OrderSG.comment, "summary": OrderSG.summary, "terms": OrderSG.terms,
}
STEP_OF = {st.state: name for name, st in STATES.items()}
SUB_STEPS = {"deadline_pick": "deadline", "terms": "summary"}
FILE_KIND = {"photos": "condition", "lecture": "lecture", "practice": "practice"}
FILE_STATES = StateFilter(OrderSG.photos, OrderSG.lecture, OrderSG.practice)
# шаги без файлов: присланное здесь фото не теряем — кладём в условие (v2.3)
OTHER_STATES = StateFilter(OrderSG.subject, OrderSG.count, OrderSG.variant, OrderSG.deadline, OrderSG.deadline_pick,
                           OrderSG.comment, OrderSG.summary, OrderSG.terms)
MAX_VARIANT = 64
MAX_COMMENT = 1000
DEADLINE_MIN = timedelta(hours=1)
DEADLINE_MAX = timedelta(days=90)
PICK_DAYS = range(3, 10)
DRAFT_LOST = "Черновик не нашли — начни заново: «📝 Заказать»."

_dropped: set[int] = set()  # кто прислал больше MAX_FILES файлов — скажем об этом в подтверждении


def is_explain(order: Order | None) -> bool:
    return svc.is_explain(order)


# ------------------------------------------------------------------ шаги
def steps_for(order: Order | None, s: RuntimeSettings) -> list[str]:
    if is_explain(order):
        return ["photos", "comment", "deadline", "summary"]
    seq = ["subject", "photos", "count", "variant", "deadline"]
    if order is not None and order.subject and s.notes_discount_for(order.subject):
        seq += ["lecture", "practice"]
    return seq + ["comment", "summary"]


def next_step(step: str, order: Order, s: RuntimeSettings) -> str:
    seq = steps_for(order, s)
    base = SUB_STEPS.get(step, step)
    i = seq.index(base) if base in seq else 0
    return seq[min(i + 1, len(seq) - 1)]


def prev_step(step: str, order: Order | None, s: RuntimeSettings) -> str:
    if step in SUB_STEPS:
        return SUB_STEPS[step]
    seq = steps_for(order, s)
    if step not in seq:
        return seq[0]
    return seq[max(seq.index(step) - 1, 0)]


def progress(step: str, order: Order | None, s: RuntimeSettings) -> str:
    """«Шаг 2 из 5 ▰▰▱▱▱» — выбор предмета и итог шагами не считаем."""
    seq = [x for x in steps_for(order, s) if x not in ("subject", "summary")]
    base = SUB_STEPS.get(step, step)
    if base not in seq:
        return ""
    i, n = seq.index(base) + 1, len(seq)
    return f"<i>Шаг {i} из {n}</i>  {'▰' * i}{'▱' * (n - i)}\n"


async def sane_step(session: AsyncSession, order: Order, step: str, s: RuntimeSettings) -> str:
    """Не пускаем дальше первого незаполненного шага (например, после смены настроек)."""
    seq = steps_for(order, s)
    base = SUB_STEPS.get(step, step)
    if base not in seq:
        step = base = "photos"
    if not order.subject:  # фото прислали раньше, чем выбрали предмет
        return "subject"
    need = None
    if await svc.count_files(session, order.id, "condition") == 0:
        need = "photos"
    elif is_explain(order) and not (order.comment or "").strip():
        need = "comment"
    elif not is_explain(order) and not order.task_count:
        need = "count"
    elif not order.deadline_at or order.deadline_at <= now_utc():
        need = "deadline"
    if need and need in seq and seq.index(need) < seq.index(base):
        return need
    return step


async def current_draft(session: AsyncSession, state: FSMContext, user_id: int) -> Order | None:
    oid = (await state.get_data()).get("order_id")
    if oid:
        order = await svc.get_order(session, int(oid))
        if order is not None and order.user_id == user_id and order.status == "draft":
            return order
    return await svc.get_draft(session, user_id)


# ------------------------------------------------------------------ клавиатуры и тексты
def nav(back: bool = True) -> list:
    row = [cb(texts.BTN_BACK, WizCb(action="back"))] if back else []
    row.append(cb(texts.BTN_CANCEL, WizCb(action="cancel")))
    return row


def files_kb(step: str, n: int, back: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if step == "photos":
        rows.append([cb("✅ Готово", WizCb(action="files_done"))])
    else:
        rows.append([cb("✅ Готово" if n else texts.BTN_SKIP, WizCb(action="files_done"))])
    if n:
        rows.append([cb(texts.BTN_CLEAR, WizCb(action="files_clear"))])
    rows.append(nav(back))
    return ik(*rows)


def remind_kb() -> InlineKeyboardMarkup:
    return ik([cb(texts.REMIND_BTN, MenuCb(action="remind"))])


def closed_text(intake: slots.IntakeState) -> str:
    return texts.ORDER_CLOSED_MANUAL if intake.reason == "manual" else texts.ORDER_CLOSED_LIMIT


def _now_value(value: str) -> str:
    return f"\n\n<i>Сейчас: {value}</i>"


def deadline_buttons(s: RuntimeSettings, now: datetime) -> list:
    """«Сегодня · ×2,0», «Завтра · ×1,5», «Через 2 дня · без наценки», «Через 3+ дня · без наценки»."""
    today = local_today(now)
    btns = []
    for code, label in texts.DEADLINE_BTNS.items():
        days = {"d0": 0, "d1": 1, "d2": 2, "d3": 3}[code]
        dt = end_of_local_day(today + timedelta(days=days))
        if dt < now + DEADLINE_MIN:
            continue  # поздно вечером «Сегодня» уже не успеть
        btns.append(cb(f"{label} · {guide.deadline_hint(s, hours_between(now, dt))}", WizCb(action="dl", value=code)))
    return btns


def price_line(order: Order, ppt: int, urgency_pct: int, total: int) -> str:
    """«💵 200 ₽ × 1 задача = 200 ₽ · срок без наценки» / «… × срочность ×1,5 = 300 ₽»."""
    parts = [fmt_rub(ppt)] if is_explain(order) else [fmt_rub(ppt), tasks_word(order.task_count or 1)]
    if urgency_pct != 100:
        parts.append(f"срочность {fmt_mult(urgency_pct)}")
    line = f"💵 {' × '.join(parts)} = <b>{fmt_rub(total)}</b>" if len(parts) > 1 else f"💵 <b>{fmt_rub(total)}</b>"
    return line + (f" · срок {texts.NO_MARKUP}" if urgency_pct == 100 else "")


async def _payment_lines(session: AsyncSession, user: User, s: RuntimeSettings, now: datetime) -> list[str]:
    lines = []
    bonus = await available_bonus(session, user.id, now)
    if bonus or user.balance_rub:
        lines.append(f"💰 Оплата: сначала бонусы (до {s.bonus_max_share_pct}% цены, у тебя {fmt_rub(bonus)}), "
                     f"потом баланс ({fmt_rub(user.balance_rub)}), остаток — счётом")
    intake = await slots.intake_state(session, s, now, unlimited=user.unlimited_active(now))
    if intake.show_remaining and intake.remaining is not None:
        lines.append(texts.SLOTS_LEFT.format(n=intake.remaining))
    return lines


async def summary_text(session: AsyncSession, order: Order, s: RuntimeSettings, user: User) -> str:
    now = now_utc()
    counts = {k: await svc.count_files(session, order.id, k) for k in ("condition", "lecture", "practice")}
    if is_explain(order):
        dp = svc.draft_price(order, s, now, False, False)
        lines = [
            texts.EXPLAIN_SUMMARY_TITLE,
            f"Фото материала: {counts['condition']}",
            f"Вопрос: <i>{html.escape(order.comment or '—')}</i>",
            f"Дедлайн: {fmt_dt(order.deadline_at, with_weekday=True)} ({fmt_left(order.deadline_at, now)})",
            "",
        ]
        if user.unlimited_active(now):
            lines.append(texts.UNLIMITED_SUMMARY)
        else:
            lines += [price_line(order, dp.price_per_task, dp.urgency_pct, dp.base),
                      *await _payment_lines(session, user, s, now), "\nСчёт пришлём после проверки материала."]
        return "\n".join(lines)
    notes_on = s.notes_discount_for(order.subject)
    dp = svc.draft_price(order, s, now, notes_on and counts["lecture"] > 0, notes_on and counts["practice"] > 0)
    lines = [
        texts.SUMMARY_TITLE,
        f"Предмет: {subject_name(order.subject)}",
        f"Задач: {order.task_count} · " + (f"вариант {html.escape(order.variant)}" if order.variant else "без варианта"),
        f"Фото условия: {counts['condition']}",
        f"Дедлайн: {fmt_dt(order.deadline_at, with_weekday=True)} ({fmt_left(order.deadline_at, now)})",
    ]
    if notes_on:
        lec = f"{counts['lecture']} фото" if counts["lecture"] else "нет"
        pr = f"{counts['practice']} фото" if counts["practice"] else "нет"
        lines.append(f"Конспекты: лекция — {lec}, практика — {pr}")
    if order.comment:
        lines.append(f"Комментарий: <i>{html.escape(order.comment)}</i>")
    lines.append("")
    if user.unlimited_active(now):
        lines.append(texts.UNLIMITED_SUMMARY)
        return "\n".join(lines)
    lines.append(price_line(order, dp.price_per_task, dp.urgency_pct, dp.base))
    if dp.potential_discount:
        lines.append(f"📚 Если конспекты пройдут проверку: −{dp.potential_discount}% → <b>{fmt_rub(dp.with_notes)}</b>")
    if free_month_active(user, now):
        lines.append(f"🎁 «Месяц бесплатно»: осталось задач — {user.free_month_tasks_left}, учтём в счёте")
    lines += await _payment_lines(session, user, s, now)
    lines.append("\n" + texts.SUMMARY_INVOICE_NOTE)
    return "\n".join(lines)


def subject_text(s: RuntimeSettings) -> str:
    lines = [texts.STEP_SUBJECT]
    for code in texts.ORDER_SUBJECTS:
        line = f"{texts.SUBJECT_ICONS.get(code, '')} {texts.SUBJECTS[code]} — {fmt_rub(s.price_for(code))} за задачу"
        if s.notes_discount_for(code):
            line += f" (до −{int(s.discount_lecture_pct) + int(s.discount_practice_pct)}% за фото конспектов)"
        lines.append(line)
    lines.append(f"<i>{guide.urgency_lines(s)[0]}</i>")
    return "\n".join(lines)


async def render(step: str, session: AsyncSession, order: Order | None, s: RuntimeSettings,
                 user: User) -> tuple[str, InlineKeyboardMarkup]:
    now = now_utc()
    head = progress(step, order, s)
    if step == "subject":
        text = subject_text(s)
        intake = await slots.intake_state(session, s, now, unlimited=user.unlimited_active(now))
        if intake.show_remaining and intake.remaining is not None:
            text += "\n" + texts.SLOTS_LEFT.format(n=intake.remaining)
        if order is not None:
            n = await svc.count_files(session, order.id, "condition")
            if n:
                text += texts.DRAFT_SUBJECT_PHOTOS.format(n=n)
        rows = [[cb(("• " if order is not None and order.subject == code else "") + subject_name(code),
                    WizCb(action="subject", value=code))] for code in texts.ORDER_SUBJECTS]
        rows.append([cb(f"{M['explain']} — не понял тему", WizCb(action="to_explain"))])
        rows.append(nav(back=False))
        return text, ik(*rows)
    assert order is not None
    explain = is_explain(order)
    if step in FILE_KIND:
        n = await svc.count_files(session, order.id, FILE_KIND[step])
        if step == "photos" and explain:
            text = (f"{texts.EXPLAIN_TITLE}\n{head}{texts.EXPLAIN_INTRO}\n"
                    f"Стоимость — {fmt_rub(s.price_for(svc.EXPLAIN))}, срочность — по сроку.\n\n"
                    f"{texts.STEP_EXPLAIN_PHOTOS}")
        elif step == "photos":
            text = head + texts.STEP_PHOTOS + "\n" + texts.STEP_PHOTOS_TIP
        elif step == "lecture":
            text = head + texts.STEP_LECTURE.format(pct=s.discount_lecture_pct)
        else:
            text = head + texts.STEP_PRACTICE.format(pct=s.discount_practice_pct)
        if n:
            text += f"\n\n<i>Уже прислано: {n}</i>"
        return text, files_kb(step, n, back=not (explain and step == "photos"))
    if step == "count":
        text = head + texts.STEP_COUNT + (_now_value(tasks_word(order.task_count)) if order.task_count else "")
        nums = [cb(str(i), WizCb(action="count", value=str(i))) for i in range(1, 7)]
        return text, ik(nums[:3], nums[3:], nav())
    if step == "variant":
        text = head + texts.STEP_VARIANT + (_now_value(html.escape(order.variant)) if order.variant else "")
        return text, ik([cb(texts.BTN_NO_VARIANT, WizCb(action="variant_none"))], nav())
    if step == "deadline":
        text = head + texts.STEP_DEADLINE + "\n\n<b>Наценка за срочность</b>\n" + "\n".join(guide.urgency_lines(s))
        if order.deadline_at:
            text += _now_value(fmt_dt(order.deadline_at, with_weekday=True))
        btns = deadline_buttons(s, now)
        return text, ik(btns[:2], btns[2:], nav())
    if step == "deadline_pick":
        today = local_today(now)
        btns = []
        for k in PICK_DAYS:
            d = today + timedelta(days=k)
            hint = guide.deadline_hint(s, hours_between(now, end_of_local_day(d)))
            label = f"{WEEKDAYS[d.weekday()]} {d:%d.%m}" + ("" if hint == texts.NO_MARKUP else f" · {hint}")
            btns.append(cb(label, WizCb(action="date", value=d.isoformat())))
        return head + texts.STEP_DEADLINE_PICK, ik(btns[:4], btns[4:], nav())
    if step == "comment":
        rows = []
        if explain:
            text = head + texts.STEP_EXPLAIN_QUESTION
            if order.comment:
                text += _now_value(html.escape(order.comment))
                rows.append([cb("✅ Оставить как есть", WizCb(action="comment_keep"))])
            rows.append([cb(texts.BTN_EXPLAIN_ALL, WizCb(action="explain_all"))])
        else:
            text = head + texts.STEP_COMMENT
            if order.comment:
                text += _now_value(html.escape(order.comment))
                rows.append([cb("✅ Оставить как есть", WizCb(action="comment_keep"))])
            rows.append([cb("⏭ Без комментария", WizCb(action="comment_skip"))])
        rows.append(nav())
        return text, ik(*rows)
    if step == "summary":
        return await summary_text(session, order, s, user), ik([cb(texts.SUBMIT_BTN, WizCb(action="submit"))], nav())
    if step == "terms":
        return (s.terms_text + "\n\n" + texts.TERMS_NEED,
                ik([cb(texts.TERMS_ACCEPT_BTN, WizCb(action="terms_ok"))], nav()))
    raise ValueError(step)


async def goto(step: str, message: Message, state: FSMContext, session: AsyncSession, order: Order | None,
               s: RuntimeSettings, user: User, note: str | None = None) -> None:
    upl.cancel_ack(user.id, "files")
    await state.set_state(STATES[step])
    await state.update_data(order_id=order.id if order is not None else None)
    if order is not None and order.wizard_step != step:
        order.wizard_step = step
        await session.commit()
    text, kb = await render(step, session, order, s, user)
    await message.answer(f"{note}\n\n{text}" if note else text, reply_markup=kb)


async def lost(message: Message, state: FSMContext) -> None:
    await state.clear()
    drafts.mark_shown()
    await message.answer(DRAFT_LOST, reply_markup=main_menu())


# ------------------------------------------------------------------ вход
async def start_wizard(message: Message, state: FSMContext, session: AsyncSession, user: User,
                       s: RuntimeSettings, config: Config, kind: str = "order") -> None:
    """kind: order — разбор задач, explain — объяснение материала."""
    await state.clear()
    drafts.mark_shown()
    now = now_utc()
    intake = await slots.intake_state(session, s, now, unlimited=user.unlimited_active(now))
    if not intake.open:
        await message.answer(closed_text(intake), reply_markup=remind_kb())
        return
    info = await drafts.draft_info(session, user.id)
    if info is not None:  # черновик с фото — не теряем: продолжить, удалить или начать заново
        text, kb = drafts.screen(info, restart_kind=kind)
        await message.answer(text, reply_markup=kb)
        return
    empty = await svc.get_draft(session, user.id)
    if empty is not None:  # пустой черновик (только выбран предмет) — начинаем с чистого листа
        await svc.delete_draft(session, empty, config.uploads_dir)
    await begin(kind, message, state, session, user, s)


async def begin(kind: str, message: Message, state: FSMContext, session: AsyncSession, user: User,
                s: RuntimeSettings) -> None:
    if kind == "explain":
        order = await svc.create_draft(session, user.id, svc.EXPLAIN, now_utc())
        order.task_count = 1
        await session.commit()
        await goto("photos", message, state, session, order, s, user)
    else:
        await goto("subject", message, state, session, None, s, user)


@menu_router.message(F.text == M["order"])
async def menu_order(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                     s: RuntimeSettings, config: Config) -> None:
    await start_wizard(message, state, session, db_user, s, config, "order")


@menu_router.message(F.text == M["explain"])
async def menu_explain(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                       s: RuntimeSettings, config: Config) -> None:
    await start_wizard(message, state, session, db_user, s, config, "explain")


@router.callback_query(MenuCb.filter(F.action.in_({"order", "explain"})))
async def cb_start(call: CallbackQuery, callback_data: MenuCb, state: FSMContext, session: AsyncSession,
                   db_user: User, s: RuntimeSettings, config: Config) -> None:
    """Кнопки «Заказать» / «Объяснить материал» из помощи и обучалки."""
    await call.answer()
    await start_wizard(call.message, state, session, db_user, s, config, callback_data.action)


@menu_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    """/cancel — выйти из любого ввода. Черновик заказа при этом не удаляем (v2.3)."""
    raw = await state.get_state()
    await state.clear()
    drafts.mark_shown()
    info = await drafts.refresh_menu(session, db_user.id)
    if raw in STEP_OF and info is not None:
        await message.answer(texts.DRAFT_CANCEL_KEPT.format(n=info.photos), reply_markup=main_menu())
    else:
        await message.answer(texts.CANCELLED, reply_markup=main_menu())


@menu_router.message(F.text.startswith(texts.DRAFT_MENU_PREFIX))
async def menu_draft_btn(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    """Кнопка нижнего меню «📝 Продолжить оформление заказа (фото: N)» — экран черновика."""
    await state.clear()
    drafts.mark_shown()
    info = await drafts.refresh_menu(session, db_user.id)
    if info is None:
        await message.answer(DRAFT_LOST, reply_markup=main_menu())
        return
    text, kb = drafts.screen(info)
    await message.answer(text, reply_markup=kb)


@router.callback_query(WizCb.filter(F.action == "resume"))
async def wiz_resume(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                     s: RuntimeSettings) -> None:
    await call.answer()
    await drop_kb(call)
    drafts.mark_shown()
    now = now_utc()
    intake = await slots.intake_state(session, s, now, unlimited=db_user.unlimited_active(now))
    if not intake.open:
        await call.message.answer(closed_text(intake), reply_markup=remind_kb())
        return
    draft = await svc.get_draft(session, db_user.id)
    if draft is None:
        await goto("subject", call.message, state, session, None, s, db_user)
        return
    step = draft.wizard_step if draft.wizard_step in STATES else "photos"
    if step == "terms":
        step = "summary"
    await goto(await sane_step(session, draft, step, s), call.message, state, session, draft, s, db_user)


@router.callback_query(WizCb.filter(F.action == "draft_del"))
async def wiz_draft_delete_ask(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    """«🗑 Удалить черновик» — только с подтверждением."""
    drafts.mark_shown()
    info = await drafts.draft_info(session, db_user.id)
    if info is None:
        await call.answer(texts.STALE_BUTTON)
        await drop_kb(call)
        return
    await call.answer()
    await show(call, texts.DRAFT_DELETE_CONFIRM.format(n=info.photos),
               ik([cb(texts.DRAFT_DELETE_YES, WizCb(action="draft_del_yes")),
                   cb(texts.DRAFT_DELETE_NO, WizCb(action="draft_keep"))]))


@router.callback_query(WizCb.filter(F.action == "draft_del_yes"))
async def wiz_draft_delete(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                           config: Config) -> None:
    drafts.mark_shown()
    await call.answer()
    await drop_kb(call)
    async with upl.user_lock(db_user.id):
        draft = await svc.get_draft(session, db_user.id)
        if draft is not None:
            await svc.delete_draft(session, draft, config.uploads_dir)
    upl.cancel_ack(db_user.id, "files")
    if (await state.get_state() or "") in STEP_OF:
        await state.clear()
    await drafts.refresh_menu(session, db_user.id)
    await call.message.answer(texts.DRAFT_DELETED, reply_markup=main_menu())


@router.callback_query(WizCb.filter(F.action == "draft_keep"))
async def wiz_draft_keep(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    drafts.mark_shown()
    info = await drafts.draft_info(session, db_user.id)
    await call.answer(texts.DRAFT_KEPT)
    if info is None:
        await drop_kb(call)
        return
    text, kb = drafts.screen(info)
    await show(call, text, kb)


@router.callback_query(WizCb.filter(F.action == "restart_ask"))
async def wiz_restart_ask(call: CallbackQuery, callback_data: WizCb, session: AsyncSession, db_user: User) -> None:
    """«🆕 Начать заново» на экране черновика — тоже с подтверждением: черновик удалится."""
    drafts.mark_shown()
    info = await drafts.draft_info(session, db_user.id)
    kind = "explain" if callback_data.value == "explain" else "order"
    await call.answer()
    if info is None:
        await drop_kb(call)
        await call.message.answer(texts.MENU_HINT, reply_markup=main_menu())
        return
    await show(call, texts.DRAFT_RESTART_CONFIRM.format(n=info.photos),
               ik([cb(texts.DRAFT_RESTART, WizCb(action="restart", value=kind)),
                   cb(texts.DRAFT_DELETE_NO, WizCb(action="draft_keep"))]))


@router.callback_query(WizCb.filter(F.action == "restart"))
async def wiz_restart(call: CallbackQuery, callback_data: WizCb, state: FSMContext, session: AsyncSession,
                      db_user: User, s: RuntimeSettings, config: Config) -> None:
    await call.answer()
    await drop_kb(call)
    drafts.mark_shown()
    async with upl.user_lock(db_user.id):
        draft = await svc.get_draft(session, db_user.id)
        if draft is not None:
            await svc.delete_draft(session, draft, config.uploads_dir)
    await drafts.refresh_menu(session, db_user.id)
    await begin("explain" if callback_data.value == "explain" else "order", call.message, state, session, db_user, s)


# ------------------------------------------------------------------ навигация
@router.callback_query(WizCb.filter(F.action == "cancel"))
async def wiz_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                     config: Config) -> None:
    """«✖️ Отмена» в оформлении: выходим, но черновик с фото сохраняем (v2.3). Пустой — удаляем."""
    raw = await state.get_state()
    await call.answer()
    await drop_kb(call)
    drafts.mark_shown()
    if raw in STEP_OF:
        upl.cancel_ack(db_user.id, "files")
        await state.clear()
        info = await drafts.draft_info(session, db_user.id)
        if info is None:
            empty = await svc.get_draft(session, db_user.id)
            if empty is not None:
                await svc.delete_draft(session, empty, config.uploads_dir)
        else:
            await drafts.refresh_menu(session, db_user.id)
            await call.message.answer(texts.DRAFT_CANCEL_KEPT.format(n=info.photos), reply_markup=main_menu())
            return
    await drafts.refresh_menu(session, db_user.id)
    await call.message.answer(texts.CANCELLED, reply_markup=main_menu())


@router.callback_query(StateFilter(OrderSG), WizCb.filter(F.action == "back"))
async def wiz_back(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                   s: RuntimeSettings) -> None:
    step = STEP_OF.get(await state.get_state() or "", "subject")
    order = await current_draft(session, state, db_user.id)
    await call.answer()
    await drop_kb(call)
    if order is None:
        await goto("subject", call.message, state, session, None, s, db_user)
        return
    await goto(prev_step(step, order, s), call.message, state, session, order, s, db_user)


# ------------------------------------------------------------------ фото и текст «не на том шаге» — не теряем (v2.3)
@router.message(OTHER_STATES, F.photo | F.document)
async def wiz_file_elsewhere(message: Message, state: FSMContext, session: AsyncSession, bot: Bot,
                             config: Config, sessionmaker: async_sessionmaker, db_user: User,
                             s: RuntimeSettings) -> None:
    """Фото или файл на шаге без файлов (предмет, количество, срок, комментарий, итог): кладём в условие
    заказа и показываем тот же шаг ещё раз. Альбом — одним подтверждением."""
    raw = await state.get_state()
    step = STEP_OF[raw]
    f = upl.user_file(message)
    if f is None:
        await message.answer(texts.STEP_FILE_BAD)
        return
    async with upl.user_lock(db_user.id):
        order = await current_draft(session, state, db_user.id)
        if order is None:
            if step != "subject":
                await lost(message, state)
                return
            order = await svc.create_draft(session, db_user.id, None, now_utc())
            await state.update_data(order_id=order.id)
    if not await save_file(bot, config, session, order.id, "condition", f, db_user.id):
        _dropped.add(db_user.id)
    caption = (message.caption or "").strip()
    if step == "comment" and caption and len(caption) <= MAX_COMMENT and not message.media_group_id:
        order.comment = caption  # подпись к фото на шаге комментария — это и есть комментарий/вопрос
        await session.commit()
        n = await svc.count_files(session, order.id, "condition")
        await goto(next_step("comment", order, s), message, state, session, order, s, db_user,
                   note=texts.DRAFT_PHOTO_SAVED.format(n=n))
        return
    chat_id, order_id, user_id = message.chat.id, order.id, db_user.id

    async def ack() -> None:
        if await state.get_state() != raw:
            return
        async with sessionmaker() as s2:
            fresh = await svc.get_order(s2, order_id)
            user = await s2.get(User, user_id)
            if fresh is None or fresh.status != "draft" or user is None:
                return
            n = await svc.count_files(s2, order_id, "condition")
            text, kb = await render(step, s2, fresh, s, user)
        note = texts.DRAFT_PHOTO_SAVED.format(n=n)
        if user_id in _dropped:
            _dropped.discard(user_id)
            note = texts.STEP_FILES_LIMIT + "\n" + note
        if step == "subject":
            note += "\n" + texts.DRAFT_NEED_SUBJECT
        await bot.send_message(chat_id, f"{note}\n\n{text}", reply_markup=kb)

    upl.schedule_ack(user_id, "files", ack)


def subject_from_text(text: str) -> str | None:
    """«начерталка», «📐 Начертательная геометрия» → код предмета (если текст — это название предмета)."""
    t = text.strip().lower().lstrip("•").strip()
    for code in texts.ORDER_SUBJECTS:
        names = {texts.SUBJECTS[code].lower(), subject_name(code).lower(), code}
        names |= {x.lower() for x in texts.SUBJECT_ALIASES.get(code, ())}
        if t in names:
            return code
    return None


@router.message(OrderSG.subject, F.text)
async def wiz_subject_text(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                           s: RuntimeSettings) -> None:
    """Текст на шаге выбора предмета: название предмета — выбираем его, иначе сохраняем в комментарий."""
    t = (message.text or "").strip()
    code = subject_from_text(t)
    if code is not None:
        await choose_subject(message, state, session, db_user, s, code)
        return
    if not t or t.startswith("/"):
        await message.answer(texts.WIZ_PRESS_BUTTON)
        return
    if len(t) > MAX_COMMENT:
        await message.answer(texts.STEP_COMMENT_LONG)
        return
    async with upl.user_lock(db_user.id):
        order = await current_draft(session, state, db_user.id)
        if order is None:
            order = await svc.create_draft(session, db_user.id, None, now_utc())
        joined = f"{order.comment}\n{t}" if order.comment else t
        order.comment = joined[:MAX_COMMENT]
        await session.commit()
    await goto("subject", message, state, session, order, s, db_user, note=texts.DRAFT_TEXT_SAVED)


@router.message(StateFilter(OrderSG.subject, OrderSG.summary, OrderSG.terms))
async def wiz_press_button(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                           s: RuntimeSettings) -> None:
    step = STEP_OF[await state.get_state()]
    order = await current_draft(session, state, db_user.id)
    if order is None and step != "subject":
        await lost(message, state)
        return
    await message.answer(texts.WIZ_PRESS_BUTTON)
    await goto(step, message, state, session, order, s, db_user)


# ------------------------------------------------------------------ предмет
@router.callback_query(OrderSG.subject, WizCb.filter(F.action == "subject"))
async def wiz_subject(call: CallbackQuery, callback_data: WizCb, state: FSMContext, session: AsyncSession,
                      db_user: User, s: RuntimeSettings) -> None:
    code = callback_data.value
    if code not in texts.ORDER_SUBJECTS:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    await drop_kb(call)
    await choose_subject(call.message, state, session, db_user, s, code)


async def choose_subject(message: Message, state: FSMContext, session: AsyncSession, user: User,
                         s: RuntimeSettings, code: str) -> None:
    async with upl.user_lock(user.id):
        order = await current_draft(session, state, user.id)
        if order is None:
            order = await svc.create_draft(session, user.id, code, now_utc())
        elif order.subject != code:
            if not s.notes_discount_for(code):
                for kind in ("lecture", "practice"):
                    await svc.clear_files(session, order.id, kind)
            order.subject = code
            await session.commit()
    await goto("photos", message, state, session, order, s, user)


@router.callback_query(OrderSG.subject, WizCb.filter(F.action == "to_explain"))
async def wiz_to_explain(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                         s: RuntimeSettings) -> None:
    """С выбора предмета — в «Объяснить материал» (черновик, если есть, превращается в заявку)."""
    await call.answer()
    await drop_kb(call)
    async with upl.user_lock(db_user.id):
        order = await current_draft(session, state, db_user.id)
        if order is None:
            order = await svc.create_draft(session, db_user.id, svc.EXPLAIN, now_utc())
        else:
            for kind in ("lecture", "practice"):
                await svc.clear_files(session, order.id, kind)
            order.subject, order.variant = svc.EXPLAIN, None
        order.task_count = 1
        await session.commit()
    await goto("photos", call.message, state, session, order, s, db_user)


# ------------------------------------------------------------------ файлы: условие/материал, лекция, практика
async def save_file(bot: Bot, config: Config, session: AsyncSession, order_id: int, kind: str,
                    f: upl.IncomingFile, user_id: int) -> bool:
    """Скачать файл и добавить к черновику. False — уже MAX_FILES файлов этого вида (файл не взяли).
    Тот же файл второй раз не добавляем (повтор альбома, «📥 Оформить заказ» после оформления)."""
    async def exists() -> bool:
        return bool(await session.scalar(select(OrderFile.id).where(
            OrderFile.order_id == order_id, OrderFile.kind == kind, OrderFile.tg_file_unique_id == f.unique_id)
            .limit(1)))

    if await exists():
        return True
    if await svc.count_files(session, order_id, kind) >= svc.MAX_FILES:
        return False
    dest = config.uploads_dir / str(order_id) / f"{kind}-{f.unique_id}{f.ext}"
    local = await upl.download(bot, f, dest)
    async with upl.user_lock(user_id):
        if await exists():
            return True
        if await svc.count_files(session, order_id, kind) >= svc.MAX_FILES:
            if local is not None:
                local.unlink(missing_ok=True)
            return False
        await svc.add_file(session, order_id, kind, **upl.file_fields(f, local))
    return True


@router.message(FILE_STATES, F.photo | F.document)
async def wiz_file(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, config: Config,
                   sessionmaker: async_sessionmaker, db_user: User) -> None:
    raw = await state.get_state()
    step = STEP_OF[raw]
    kind = FILE_KIND[step]
    order = await current_draft(session, state, db_user.id)
    if order is None:
        await lost(message, state)
        return
    f = upl.user_file(message)
    if f is None:
        await message.answer(texts.STEP_FILE_BAD)
        return
    if not await save_file(bot, config, session, order.id, kind, f, db_user.id):
        _dropped.add(db_user.id)
    chat_id, order_id, user_id = message.chat.id, order.id, db_user.id
    back = not (is_explain(order) and step == "photos")

    async def ack() -> None:
        if await state.get_state() != raw:
            return
        async with sessionmaker() as s2:
            n = await svc.count_files(s2, order_id, kind)
        text = (texts.STEP_PHOTOS_ACK if step == "photos" else texts.STEP_NOTES_ACK).format(n=n)
        if user_id in _dropped:
            _dropped.discard(user_id)
            text = texts.STEP_FILES_LIMIT + "\n" + text
        await bot.send_message(chat_id, text, reply_markup=files_kb(step, n, back))

    upl.schedule_ack(user_id, "files", ack)


@router.message(FILE_STATES)
async def wiz_file_other(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    step = STEP_OF[await state.get_state()]
    order = await current_draft(session, state, db_user.id)
    if order is None:
        await lost(message, state)
        return
    n = await svc.count_files(session, order.id, FILE_KIND[step])
    await message.answer(texts.TEXT_EXPECTED_FILES if message.text else texts.STEP_FILE_BAD,
                         reply_markup=files_kb(step, n, back=not (is_explain(order) and step == "photos")))


@router.callback_query(FILE_STATES, WizCb.filter(F.action == "files_done"))
async def wiz_files_done(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                         s: RuntimeSettings) -> None:
    step = STEP_OF[await state.get_state()]
    order = await current_draft(session, state, db_user.id)
    if order is None:
        await call.answer()
        await lost(call.message, state)
        return
    if step == "photos" and await svc.count_files(session, order.id, "condition") == 0:
        await call.answer(texts.STEP_PHOTOS_NEED, show_alert=True)
        return
    await call.answer()
    await drop_kb(call)
    await goto(next_step(step, order, s), call.message, state, session, order, s, db_user)


@router.callback_query(FILE_STATES, WizCb.filter(F.action == "files_clear"))
async def wiz_files_clear(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                          s: RuntimeSettings) -> None:
    step = STEP_OF[await state.get_state()]
    order = await current_draft(session, state, db_user.id)
    if order is None:
        await call.answer()
        await lost(call.message, state)
        return
    async with upl.user_lock(db_user.id):
        await svc.clear_files(session, order.id, FILE_KIND[step])
    await call.answer("Очистили 🗑")
    await drop_kb(call)
    await goto(step, call.message, state, session, order, s, db_user)


# ------------------------------------------------------------------ количество и вариант
async def _set_count(message: Message, state: FSMContext, session: AsyncSession, user: User,
                     s: RuntimeSettings, n: int) -> None:
    order = await current_draft(session, state, user.id)
    if order is None:
        await lost(message, state)
        return
    now = now_utc()
    intake = await slots.intake_state(session, s, now, unlimited=user.unlimited_active(now))
    if intake.remaining is not None and n > intake.remaining:
        await message.answer(texts.NOT_ENOUGH_SLOTS.format(n=intake.remaining))
        return
    order.task_count = n
    await session.commit()
    await goto("variant", message, state, session, order, s, user)


@router.callback_query(OrderSG.count, WizCb.filter(F.action == "count"))
async def wiz_count_btn(call: CallbackQuery, callback_data: WizCb, state: FSMContext, session: AsyncSession,
                        db_user: User, s: RuntimeSettings) -> None:
    n = int(callback_data.value) if callback_data.value.isdigit() else 0
    if not 1 <= n <= svc.MAX_TASKS:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    await drop_kb(call)
    await _set_count(call.message, state, session, db_user, s, n)


@router.message(OrderSG.count)
async def wiz_count_text(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                         s: RuntimeSettings) -> None:
    t = (message.text or "").strip()
    if not t.isdigit() or not 1 <= int(t) <= svc.MAX_TASKS:
        await message.answer(texts.STEP_COUNT_BAD)
        return
    await _set_count(message, state, session, db_user, s, int(t))


async def _set_variant(message: Message, state: FSMContext, session: AsyncSession, user: User,
                       s: RuntimeSettings, variant: str | None) -> None:
    order = await current_draft(session, state, user.id)
    if order is None:
        await lost(message, state)
        return
    order.variant = variant
    await session.commit()
    await goto("deadline", message, state, session, order, s, user)


@router.callback_query(OrderSG.variant, WizCb.filter(F.action == "variant_none"))
async def wiz_variant_none(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                           s: RuntimeSettings) -> None:
    await call.answer()
    await drop_kb(call)
    await _set_variant(call.message, state, session, db_user, s, None)


@router.message(OrderSG.variant)
async def wiz_variant_text(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                           s: RuntimeSettings) -> None:
    t = (message.text or "").strip()
    if not t:
        await message.answer("Напиши номер варианта текстом или нажми «Без варианта».")
        return
    if len(t) > MAX_VARIANT:
        await message.answer(f"Слишком длинно — уложись в {MAX_VARIANT} символа.")
        return
    await _set_variant(message, state, session, db_user, s, t)


# ------------------------------------------------------------------ дедлайн
async def _set_deadline(message: Message, state: FSMContext, session: AsyncSession, user: User,
                        s: RuntimeSettings, dt: datetime) -> None:
    now = now_utc()
    if dt < now + DEADLINE_MIN:
        await message.answer(texts.STEP_DEADLINE_PAST)
        return
    if dt > now + DEADLINE_MAX:
        await message.answer(texts.STEP_DEADLINE_FAR)
        return
    order = await current_draft(session, state, user.id)
    if order is None:
        await lost(message, state)
        return
    order.deadline_at = dt
    await session.commit()
    hint = guide.deadline_hint(s, hours_between(now, dt))
    note = f"✅ Срок: {fmt_dt(dt, with_weekday=True)} — {hint}"
    await goto(next_step("deadline", order, s), message, state, session, order, s, user, note=note)


@router.callback_query(OrderSG.deadline, WizCb.filter(F.action == "dl"))
async def wiz_deadline_btn(call: CallbackQuery, callback_data: WizCb, state: FSMContext, session: AsyncSession,
                           db_user: User, s: RuntimeSettings) -> None:
    code = callback_data.value
    if code == "d3":
        await call.answer()
        await drop_kb(call)
        order = await current_draft(session, state, db_user.id)
        if order is None:
            await lost(call.message, state)
            return
        await goto("deadline_pick", call.message, state, session, order, s, db_user)
        return
    days = {"d0": 0, "d1": 1, "d2": 2}.get(code)
    if days is None:
        await call.answer(texts.STALE_BUTTON)
        return
    dt = end_of_local_day(local_today() + timedelta(days=days))
    if dt < now_utc() + DEADLINE_MIN:
        await call.answer(texts.STEP_DEADLINE_PAST, show_alert=True)
        return
    await call.answer()
    await drop_kb(call)
    await _set_deadline(call.message, state, session, db_user, s, dt)


@router.callback_query(OrderSG.deadline_pick, WizCb.filter(F.action == "date"))
async def wiz_deadline_date(call: CallbackQuery, callback_data: WizCb, state: FSMContext, session: AsyncSession,
                            db_user: User, s: RuntimeSettings) -> None:
    try:
        d = date.fromisoformat(callback_data.value)
    except ValueError:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    await drop_kb(call)
    await _set_deadline(call.message, state, session, db_user, s, end_of_local_day(d))


@router.message(StateFilter(OrderSG.deadline, OrderSG.deadline_pick))
async def wiz_deadline_text(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                            s: RuntimeSettings) -> None:
    dt = parse_deadline(message.text or "", now_utc())
    if dt is None:
        await message.answer(texts.STEP_DEADLINE_BAD)
        return
    await _set_deadline(message, state, session, db_user, s, dt)


# ------------------------------------------------------------------ комментарий / вопрос
async def _after_comment(message: Message, state: FSMContext, session: AsyncSession, user: User,
                         s: RuntimeSettings, comment: str | None, keep: bool = False) -> None:
    order = await current_draft(session, state, user.id)
    if order is None:
        await lost(message, state)
        return
    if not keep:
        order.comment = comment
        await session.commit()
    await goto(next_step("comment", order, s), message, state, session, order, s, user)


@router.callback_query(OrderSG.comment, WizCb.filter(F.action.in_({"comment_skip", "comment_keep", "explain_all"})))
async def wiz_comment_btn(call: CallbackQuery, callback_data: WizCb, state: FSMContext, session: AsyncSession,
                          db_user: User, s: RuntimeSettings) -> None:
    order = await current_draft(session, state, db_user.id)
    if order is None:
        await call.answer()
        await lost(call.message, state)
        return
    action = callback_data.action
    if is_explain(order) and (action == "comment_skip" or (action == "comment_keep" and not order.comment)):
        await call.answer(texts.STEP_EXPLAIN_QUESTION_NEED, show_alert=True)
        return
    await call.answer()
    await drop_kb(call)
    comment = svc.EXPLAIN_ALL if action == "explain_all" else None
    await _after_comment(call.message, state, session, db_user, s, comment, keep=action == "comment_keep")


@router.message(OrderSG.comment)
async def wiz_comment_text(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                           s: RuntimeSettings) -> None:
    t = (message.text or message.caption or "").strip()
    if not t:
        order = await current_draft(session, state, db_user.id)
        await message.answer(texts.STEP_EXPLAIN_QUESTION_NEED if is_explain(order)
                             else "Напиши комментарий текстом или нажми «Без комментария».")
        return
    if len(t) > MAX_COMMENT:
        await message.answer(texts.STEP_COMMENT_LONG)
        return
    await _after_comment(message, state, session, db_user, s, t)


# ------------------------------------------------------------------ итог, условия, отправка
@router.callback_query(OrderSG.summary, WizCb.filter(F.action == "submit"))
async def wiz_submit(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                     s: RuntimeSettings, bot: Bot, config: Config) -> None:
    order = await current_draft(session, state, db_user.id)
    await call.answer()
    await drop_kb(call)
    if order is None:
        await lost(call.message, state)
        return
    if db_user.terms_accepted_at is None:
        await goto("terms", call.message, state, session, order, s, db_user)
        return
    await submit(call.message, state, session, db_user, s, bot, config, order)


@router.callback_query(OrderSG.terms, WizCb.filter(F.action == "terms_ok"))
async def wiz_terms_ok(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                       s: RuntimeSettings, bot: Bot, config: Config) -> None:
    order = await current_draft(session, state, db_user.id)
    if db_user.terms_accepted_at is None:
        db_user.terms_accepted_at = now_utc()
        await session.commit()
    await call.answer("Принято ✅")
    await drop_kb(call)
    await call.message.answer(texts.TERMS_ACCEPTED.format(date=fmt_dt(db_user.terms_accepted_at)))
    if order is None:
        await lost(call.message, state)
        return
    await submit(call.message, state, session, db_user, s, bot, config, order)


async def submit(message: Message, state: FSMContext, session: AsyncSession, user: User, s: RuntimeSettings,
                 bot: Bot, config: Config, draft: Order) -> None:
    try:
        order = await svc.submit_order(session, draft.id, s, now_utc())
    except svc.IntakeClosed as e:
        await message.answer(closed_text(e.state), reply_markup=remind_kb())
        return
    except svc.NotEnoughSlots as e:
        await message.answer(texts.NOT_ENOUGH_SLOTS.format(n=e.remaining))
        await goto("count", message, state, session, draft, s, user)
        return
    except svc.OrderError as e:
        await message.answer(html.escape(str(e)))
        again = await svc.get_order(session, draft.id)
        if again is None or again.status != "draft":
            await state.clear()
            return
        await goto(await sane_step(session, again, "summary", s), message, state, session, again, s, user)
        return
    await state.clear()
    drafts.mark_shown()
    await drafts.refresh_menu(session, user.id)  # черновика больше нет — кнопка «Продолжить» уходит из меню
    notes = "pending" in (order.lecture_status, order.practice_status)
    if order.unlimited:
        await message.answer(texts.SUBMITTED_UNLIMITED.format(id=order.id), reply_markup=main_menu())
    elif is_explain(order):
        await message.answer(texts.EXPLAIN_SUBMITTED.format(id=order.id), reply_markup=main_menu())
    else:
        await message.answer(texts.SUBMITTED.format(id=order.id, notes=" и конспекты" if notes else ""),
                             reply_markup=main_menu())
    await notify_new_order(bot, session, config, s, order, user, notes)


async def notify_new_order(bot: Bot, session: AsyncSession, config: Config, s: RuntimeSettings, order: Order,
                           user: User, notes: bool) -> None:
    """Админам: сначала файлы (если включено), ниже — заголовок с кнопками. Ответ свайпом на заголовок уйдёт
    клиенту как сообщение по заказу."""
    deadline = fmt_dt(order.deadline_at, with_weekday=True)
    if is_explain(order):
        text = texts.ADMIN_NEW_EXPLAIN.format(id=order.id, user=display_name(user), deadline=deadline)
        text += f"\n❓ <i>{html.escape((order.comment or '')[:700])}</i>"
    else:
        text = texts.ADMIN_NEW_ORDER.format(
            id=order.id, user=display_name(user), subject=subject_name(order.subject), count=order.task_count,
            deadline=deadline, notes=texts.ADMIN_NEW_NOTES if notes else "")
        if order.variant:
            text += f"\n🔖 Вариант: {html.escape(order.variant)}"
        if order.comment:
            text += f"\n💬 <i>{html.escape(order.comment[:500])}</i>"
    if order.unlimited:
        text += f"\n{texts.ADMIN_ORDER_UNLIMITED} · ⏰ {fmt_left(order.deadline_at)}"
    else:
        text += f"\n💵 По расчёту: {fmt_rub(order.price or 0)} · ⏰ {fmt_left(order.deadline_at)}"
    if user.is_priority:
        text += " · ⭐ приоритет"
    text += "\n" + texts.ADMIN_REPLY_HINT.strip()
    if s.admin_autosend_files:
        groups = [("condition", "📸 Материал" if is_explain(order) else "📄 Условие"),
                  ("lecture", "📗 Лекция"), ("practice", "📘 Практика")]
        files = {kind: await svc.files_of(session, order.id, kind) for kind, _ in groups}
        for admin_id, _lvl in staff_targets(config, 1):
            for kind, title in groups:
                if not files[kind]:
                    continue
                try:
                    await send_files(bot, admin_id, files[kind], caption=f"{title} · #{order.id}")
                except (TelegramBadRequest, TelegramForbiddenError) as e:
                    log.warning("файлы заказа #%s админу %s не ушли: %s", order.id, admin_id, e)
                    break
    sent = await notify_admins(bot, config, text, markup_for=lambda _lvl: order_alert_kb(order.id))
    remember_all(session, sent, "order", order.id, user.id, text)
    await session.commit()
