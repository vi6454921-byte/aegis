"""«📦 Мои заказы»: список, карточка с подсказкой «что дальше», оплата, решение, «Зачли / Не зачли», отмена."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot import files as upl
from app.bot.callbacks import AdminCb, MyOrderCb, PayCb, SupCb
from app.bot.handlers.common import menu_router
from app.bot.keyboards import cb, ik, main_menu, order_alert_kb, pager, solution_button
from app.bot.notify import mark_headers, notify_admins, remember_all, send_files, staff_targets
from app.bot.designs import result_kb
from app.bot.render import order_card, order_line, status_label
from app.bot.states import DisputeSG
from app.bot.ui import drop_kb, show
from app.config import Config
from app.db.models import Order, User
from app.services import design as dsvc
from app.services import orders as svc
from app.services import solutions
from app.services.users import display_name
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc, to_local

log = logging.getLogger(__name__)
router = Router(name="my_orders")
M = texts.MAIN_MENU
PER_PAGE = 8
SOLUTION_VISIBLE = ("done", "accepted", "dispute", "in_work")
STATUS_CHANGED = "Статус заказа уже изменился: открой его в «📦 Мои заказы»."


def solution_visible(order: Order) -> bool:
    return bool(order.solution_entry and order.done_at and order.status in SOLUTION_VISIBLE)


def status_hint(order: Order) -> str | None:
    if svc.is_explain(order) and order.status in texts.EXPLAIN_STATUS_HINT:
        return texts.EXPLAIN_STATUS_HINT[order.status]
    if svc.is_any(order) and order.status in texts.ANY_STATUS_HINT:
        return texts.ANY_STATUS_HINT[order.status]
    if dsvc.is_design(order):
        return texts.DESIGN_STATUS_HINT.get(order.status) or texts.STATUS_HINT.get(order.status)
    hint = texts.STATUS_HINT.get(order.status)
    if hint and "{notes}" in hint:
        pending = order.lecture_status == "pending" or order.practice_status == "pending"
        hint = hint.format(notes=" и конспекты" if pending else "")
    return hint


async def list_view(session: AsyncSession, user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    rows, total = await svc.user_orders(session, user_id, page, PER_PAGE)
    draft = await svc.get_draft(session, user_id)
    if not rows and draft is None:
        return texts.MY_ORDERS_EMPTY, None
    lines = ["📦 <b>Мои заказы</b>", ""]
    if draft is not None:
        what = M["explain"] if svc.is_explain(draft) else M["order"]
        lines.append(f"📝 Есть незаконченная заявка, продолжить можно через «{what}».")
    buttons = []
    for o in rows:
        lines.append(order_line(o))
        buttons.append([cb(f"#{o.id} · {status_label(o)}",
                           MyOrderCb(action="card", id=o.id, page=page))])
    if rows:
        lines.append("\n<i>Нажми на заказ, чтобы открыть подробности.</i>")
    return "\n".join(lines), ik(*buttons, pager(page, total, PER_PAGE, lambda p: MyOrderCb(action="list", page=p)))


def card_kb(order: Order, config: Config, page: int) -> InlineKeyboardMarkup:
    explain = svc.is_explain(order)
    rows = []
    if order.status in svc.PAYABLE and order.price_final:
        rows.append([cb(f"{texts.PAY_BTN} · {fmt_rub(order.price_final)}", PayCb(action="order", id=order.id))])
    if solution_visible(order):
        rows.append([solution_button(config, order.solution_token, MyOrderCb(action="solution", id=order.id))])
    if order.status == "done" and dsvc.is_design(order):  # v2.4: «✅ Принять» / «🔁 Нужны правки»
        rows += result_kb(order.id).inline_keyboard
    elif order.status == "done":
        if explain:
            rows.append([cb(texts.EXPLAIN_ACCEPT_BTN, MyOrderCb(action="accept", id=order.id, page=page)),
                         cb(texts.EXPLAIN_ASK_BTN, SupCb(action="order", id=order.id))])
        else:
            rows.append([cb(texts.ACCEPT_BTN, MyOrderCb(action="accept", id=order.id, page=page)),
                         cb(texts.DISPUTE_BTN, MyOrderCb(action="dispute", id=order.id, page=page))])
    if order.status != "cancelled" and not (explain and order.status == "done"):
        rows.append([cb("💬 Вопрос по заказу", SupCb(action="order", id=order.id))])
    if order.status in svc.CANCELLABLE:
        label = texts.QUOTE_DECLINE_BTN if order.status == svc.QUOTED else "✖️ Отменить заказ"
        rows.append([cb(label, MyOrderCb(action="cancel", id=order.id, page=page))])
    rows.append([cb("⬅️ К списку", MyOrderCb(action="list", page=page))])
    return ik(*rows)


async def card_text(session: AsyncSession, order: Order) -> str:
    counts = {k: await svc.count_files(session, order.id, k) for k in ("condition", "lecture", "practice")}
    text = order_card(order, counts)
    hint = status_hint(order)
    return text + (f"\n\n{hint}" if hint else "")


async def own_order(session: AsyncSession, order_id: int, user: User) -> Order | None:
    order = await svc.get_order(session, order_id)
    if order is None or order.user_id != user.id or order.status == "draft":
        return None
    return order


def done_kb(order: Order, config: Config) -> InlineKeyboardMarkup:
    """Кнопки под сообщением «решение готово»."""
    return ik([solution_button(config, order.solution_token, MyOrderCb(action="solution", id=order.id))],
              [cb(texts.ACCEPT_BTN, MyOrderCb(action="accept", id=order.id)),
               cb(texts.DISPUTE_BTN, MyOrderCb(action="dispute", id=order.id))])


async def send_solution_files(bot: Bot, session: AsyncSession, config: Config, order: Order, chat_id: int) -> None:
    """Без Mini App решение уходит документами (те же файлы, что прислал исполнитель)."""
    title = f"{'📸 Объяснение' if svc.is_explain(order) else '🧩 Решение'} по заказу #{order.id}"
    files = await svc.files_of(session, order.id, "solution")
    if files:
        await send_files(bot, chat_id, files, caption=title)
        return
    path = solutions.resolve(config.solutions_dir, order.id, order.solution_entry or "")
    if path is not None:
        await bot.send_document(chat_id, FSInputFile(path), caption=title)


# ------------------------------------------------------------------ список и карточка
@menu_router.message(F.text == M["my_orders"])
async def menu_my_orders(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await state.clear()
    text, kb = await list_view(session, db_user.id, 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(MyOrderCb.filter(F.action == "list"))
async def cb_list(call: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, db_user: User) -> None:
    await call.answer()
    text, kb = await list_view(session, db_user.id, max(callback_data.page, 0))
    await show(call, text, kb)


@router.callback_query(MyOrderCb.filter(F.action == "card"))
async def cb_card(call: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, db_user: User,
                  config: Config) -> None:
    order = await own_order(session, callback_data.id, db_user)
    if order is None:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await show(call, await card_text(session, order), card_kb(order, config, callback_data.page))


@router.callback_query(MyOrderCb.filter(F.action == "solution"))
async def cb_solution(call: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, db_user: User,
                      bot: Bot, config: Config) -> None:
    order = await own_order(session, callback_data.id, db_user)
    if order is None or not solution_visible(order):
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await send_solution_files(bot, session, config, order, call.from_user.id)


# ------------------------------------------------------------------ отмена
@router.callback_query(MyOrderCb.filter(F.action == "cancel"))
async def cb_cancel(call: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, db_user: User) -> None:
    order = await own_order(session, callback_data.id, db_user)
    if order is None or order.status not in svc.CANCELLABLE:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    await call.answer()
    quoted = order.status == svc.QUOTED
    await show(call, (texts.QUOTE_DECLINE_CONFIRM if quoted else texts.CANCEL_CONFIRM).format(id=order.id), ik(
        [cb(texts.QUOTE_DECLINE_YES if quoted else "Да, отменить",
            MyOrderCb(action="cancel_yes", id=order.id, page=callback_data.page)),
         cb("Нет", MyOrderCb(action="card", id=order.id, page=callback_data.page))]))


@router.callback_query(MyOrderCb.filter(F.action == "cancel_yes"))
async def cb_cancel_yes(call: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, db_user: User,
                        bot: Bot, config: Config) -> None:
    before = await own_order(session, callback_data.id, db_user)
    declined = before is not None and svc.is_any(before)
    try:
        order = await svc.cancel_by_user(session, callback_data.id, db_user.id, now_utc())
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer((texts.QUOTE_DECLINED if declined else texts.ORDER_CANCELLED).format(id=order.id))
    await show(call, await card_text(session, order), card_kb(order, config, callback_data.page))
    await mark_headers(bot, session, config, "order", order.id,
                       f"✖️ Клиент {'отказался' if declined else 'отменил'} · {to_local(now_utc()):%H:%M}",
                       markup_for=lambda _lvl: order_alert_kb(order.id))
    await notify_admins(bot, config, (texts.ADMIN_QUOTE_DECLINED if declined else texts.ADMIN_CANCELLED).format(
                            user=display_name(db_user), id=order.id),
                        markup_for=lambda _lvl: ik([cb("📦 Открыть заказ", AdminCb(s="o", a="open", id=order.id))]))


# ------------------------------------------------------------------ «Зачли» / «Всё понятно» / «Не зачли»
@router.callback_query(MyOrderCb.filter(F.action == "accept"))
async def cb_accept(call: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, db_user: User,
                    bot: Bot, config: Config) -> None:
    try:
        order = await svc.accept(session, callback_data.id, db_user.id, now_utc())
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    explain = svc.is_explain(order)
    await call.answer()
    await drop_kb(call)
    kb = None
    if order.solution_entry:
        kb = ik([solution_button(config, order.solution_token, MyOrderCb(action="solution", id=order.id))])
    await call.message.answer(texts.EXPLAIN_ACCEPTED if explain else texts.ACCEPTED, reply_markup=kb)
    what = f"🎓 Объяснение #{order.id}: клиенту всё понятно" if explain else f"🎓 Заказ #{order.id} зачли"
    await mark_headers(bot, session, config, "order", order.id, what, markup_for=lambda _lvl: order_alert_kb(order.id))
    await notify_admins(bot, config, f"{what}: {display_name(db_user)}.")


@router.callback_query(MyOrderCb.filter(F.action == "dispute"))
async def cb_dispute(call: CallbackQuery, callback_data: MyOrderCb, state: FSMContext, session: AsyncSession,
                     db_user: User) -> None:
    order = await own_order(session, callback_data.id, db_user)
    if order is None or order.status != "done" or svc.is_explain(order):
        await call.answer(STATUS_CHANGED, show_alert=True)
        return
    await call.answer()
    await state.set_state(DisputeSG.screenshot)
    await state.update_data(order_id=order.id)
    await call.message.answer(texts.DISPUTE_PROMPT, reply_markup=ik(
        [cb(texts.BTN_CANCEL, MyOrderCb(action="dispute_cancel", id=order.id))]))


@router.callback_query(MyOrderCb.filter(F.action == "dispute_cancel"))
async def cb_dispute_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await drop_kb(call)
    await call.message.answer(texts.CANCELLED, reply_markup=main_menu())


def dispute_kb(order_id: int, level: int) -> InlineKeyboardMarkup:
    """Кнопки для админов: 1 уровень - переделать, 2 уровень - ещё и вернуть деньги."""
    first = [cb("🔁 Переделать", AdminCb(s="o", a="redo", id=order_id))]
    if level >= 2:
        first.append(cb("↩️ Вернуть деньги", AdminCb(s="o", a="refund", id=order_id)))
    return ik(first, [cb("📦 Карточка заказа", AdminCb(s="o", a="open", id=order_id))])


@router.message(DisputeSG.screenshot, F.photo | F.document)
async def dispute_file(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                       config: Config) -> None:
    order_id = int((await state.get_data()).get("order_id") or 0)
    f = upl.user_file(message)
    if f is None:
        await message.answer(texts.STEP_FILE_BAD)
        return
    async with upl.user_lock(db_user.id):
        order = await own_order(session, order_id, db_user)
        if order is None or order.status not in ("done", "dispute"):
            await state.clear()
            await message.answer(STATUS_CHANGED, reply_markup=main_menu())
            return
        first = order.status == "done"
        if first:
            order = await svc.open_dispute(session, order.id, db_user.id, now_utc())
        local = await upl.download(bot, f, config.uploads_dir / str(order.id) / f"dispute-{f.unique_id}{f.ext}")
        await svc.add_file(session, order.id, "dispute", **upl.file_fields(f, local))
    if first:
        header = texts.ADMIN_DISPUTE.format(id=order.id, user=display_name(db_user))
        sent = await notify_admins(bot, config, header, markup_for=lambda lvl: dispute_kb(order.id, lvl))
        remember_all(session, sent, "dispute", order.id, db_user.id, header)
        await session.commit()
        await mark_headers(bot, session, config, "order", order.id, "⚠️ Клиент: «Не зачли», спор",
                           markup_for=lambda _lvl: order_alert_kb(order.id))
    for admin_id, level in staff_targets(config):
        try:
            await bot.copy_message(admin_id, message.chat.id, message.message_id,
                                   reply_markup=dispute_kb(order.id, level))
        except Exception:  # noqa: BLE001
            log.warning("не удалось переслать скрин админу %s", admin_id)

    async def ack() -> None:
        await state.clear()
        await bot.send_message(message.chat.id, texts.DISPUTE_SENT, reply_markup=main_menu())

    upl.schedule_ack(db_user.id, "dispute", ack)


@router.message(DisputeSG.screenshot)
async def dispute_other(message: Message) -> None:
    await message.answer(texts.DISPUTE_NEED_FILE)
