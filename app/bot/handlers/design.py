"""v2.4: «🎨 Файлы и дизайн»: презентация, PDF, картинка, баннер, QR-код по фиксированной цене.

Вход: кнопка в главном меню и на экране «📝 Заказать» (выбор предмета). Шаги: тип → (у презентации оформление:
свой дизайн или шаблон Самарского университета, у шаблона формат 16:9/4:3 и язык) → «Сколько штук?» →
задание (текст, фото, файлы, несколькими сообщениями) → «✅ Готово» → итог с ценой и кнопками
«💳 Оплатить» / «✏️ Изменить» / «❌ Отмена». Выбор живёт в данных состояния (data["dz"]), заказ создаётся
только на «💳 Оплатить», сразу в статусе «ждёт оплаты», и дальше оплачивается как обычно (бонусы, баланс, счёт).

Готовый файл админ присылает через «✉️ Ответить» (app/bot/conversation.py → app/bot/designs.py), клиент
отвечает «✅ Принять» или «🔁 Нужны правки»: правки уходят тому же исполнителю."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import actionlog, texts
from app.bot import designs
from app.bot import files as upl
from app.bot.callbacks import DesignCb
from app.bot.handlers import order as wizard
from app.bot.handlers import payments as pay
from app.bot.handlers.common import menu_router
from app.bot.handlers.support import deliver
from app.bot.keyboards import cb, ik, main_menu
from app.bot.notify import mark_headers, notify_admins
from app.bot.states import DesignSG
from app.bot.support import MAX_TEXT, content_type, copyable, message_text
from app.bot.ui import drop_kb, show
from app.config import Config
from app.db.models import OrderFile, User
from app.services import design as dsvc
from app.services import orders as svc
from app.services import payments as paysvc
from app.services.ledger import available_bonus
from app.services.settings import RuntimeSettings
from app.services.users import display_name
from app.utils.money import fmt_rub
from app.utils.timeutil import fmt_dt, now_utc, to_local

log = logging.getLogger(__name__)
router = Router(name="design")
M = texts.MAIN_MENU
KEY = "dz"
STEPS = {"kind": DesignSG.kind, "style": DesignSG.style, "fmt": DesignSG.fmt, "lang": DesignSG.lang,
         "qty": DesignSG.qty, "qty_input": DesignSG.qty_input, "content": DesignSG.content,
         "summary": DesignSG.summary, "terms": DesignSG.terms}
PICK_STATES = StateFilter(DesignSG.kind, DesignSG.style, DesignSG.fmt, DesignSG.lang, DesignSG.qty,
                          DesignSG.summary, DesignSG.terms)
STATUS_CHANGED = "Статус заказа уже изменился: открой его в «📦 Мои заказы»."
_dropped: set[int] = set()  # кому не взяли файл сверх лимита (сообщаем в подтверждении пачки)


def btn(label: str, a: str, v: str = "", id: int = 0):  # noqa: A002, ANN201
    return cb(label, DesignCb(a=a, v=v, id=id))


def back_btn(to: str):  # noqa: ANN201
    return btn(texts.BTN_BACK_ARROW, "back", to)


def fmt_code(fmt: str) -> str:
    """«16:9» → «16x9»: двоеточие в callback_data занято разделителем."""
    return fmt.replace(":", "x")


def fmt_of(code: str) -> str:
    return code.replace("x", ":")


# ------------------------------------------------------------------ данные выбора
async def get_spec(state: FSMContext) -> dict:
    return dict((await state.get_data()).get(KEY) or {})


async def put_spec(state: FSMContext, spec: dict) -> None:
    await state.update_data({KEY: spec})


def sent_what(spec: dict) -> str:
    """«файлов: 2, текст задания» или пусто."""
    parts = []
    if spec.get("files"):
        parts.append(f"файлов: {len(spec['files'])}")
    if (spec.get("text") or "").strip():
        parts.append(texts.DESIGN_WHAT_TEXT)
    return ", ".join(parts)


def head(spec: dict, with_qty: bool = False) -> str:
    """«<b>📊 Презентация</b>\\n🎓 Шаблон Самарского университета, 16:9, русский»."""
    lines = [f"<b>{dsvc.KINDS.get(spec.get('kind') or '', '🎨 Файлы и дизайн')}</b>"]
    style = dsvc.spec_style(spec)
    if style:
        lines.append(style)
    if with_qty and spec.get("qty"):
        lines.append(f"Количество: {spec['qty']}")
    return "\n".join(lines)


def prev_of(step: str, spec: dict) -> str:
    """Куда ведёт «← Назад» с шага."""
    if step == "qty":
        if spec.get("kind") != "pres":
            return "kind"
        return "lang" if spec.get("style") == "ssau" else "style"
    return {"style": "kind", "fmt": "style", "lang": "fmt", "qty_input": "qty", "content": "qty",
            "terms": "summary"}.get(step, "kind")


# ------------------------------------------------------------------ экраны
def kind_kb() -> InlineKeyboardMarkup:
    k = dsvc.KINDS
    return ik([btn(k["pres"], "kind", "pres"), btn(k["pdf"], "kind", "pdf")],
              [btn(k["img"], "kind", "img"), btn(k["banner"], "kind", "banner")],
              [btn(k["qr"], "kind", "qr"), back_btn("origin")])


def content_kb(has: bool) -> InlineKeyboardMarkup:
    return ik([btn(texts.ANY_DONE_BTN, "done")],
              [btn(texts.BTN_CLEAR, "clear")] if has else None,
              [back_btn("qty"), btn(texts.DESIGN_CANCEL_BTN, "cancel")])


async def summary_view(session: AsyncSession, spec: dict, user: User, s: RuntimeSettings
                       ) -> tuple[str, InlineKeyboardMarkup]:
    kind, qty = spec["kind"], int(spec["qty"])
    lines = [f"Что: {dsvc.KINDS[kind]}"]
    style = dsvc.spec_style(spec)
    if style:
        lines.append(f"Оформление: {style}")
    lines.append(f"Количество: {qty}")
    lines.append(f"Задание: {sent_what(spec)}")
    text = (spec.get("text") or "").strip()
    if text:
        lines.append(f"<i>{html.escape(text[:300])}{'…' if len(text) > 300 else ''}</i>")
    lines += ["", dsvc.price_line(kind, qty)]
    now = now_utc()
    unlimited = user.unlimited_active(now)
    if unlimited:
        lines.append(texts.DESIGN_UNLIMITED_LINE)
    else:
        bonus = await available_bonus(session, user.id, now)
        if bonus or user.balance_rub:
            lines.append(f"💰 Оплата: сначала бонусы (до {s.bonus_max_share_pct}% цены, у тебя {fmt_rub(bonus)}), "
                         f"потом баланс ({fmt_rub(user.balance_rub)}), остаток счётом")
    pay_label = texts.DESIGN_SEND_UNLIMITED_BTN if unlimited else texts.DESIGN_PAY_BTN
    return (texts.DESIGN_SUMMARY.format(lines="\n".join(lines)),
            ik([btn(pay_label, "pay")], [btn(texts.DESIGN_EDIT_BTN, "edit"), btn(texts.DESIGN_CANCEL_BTN, "cancel")]))


async def render(step: str, session: AsyncSession, spec: dict, user: User, s: RuntimeSettings
                 ) -> tuple[str, InlineKeyboardMarkup]:
    if step == "kind":
        return texts.DESIGN_SECTION, kind_kb()
    if step == "style":
        st = dsvc.STYLES
        return texts.DESIGN_STYLE_STEP, ik([btn(st["custom"], "style", "custom")], [btn(st["ssau"], "style", "ssau")],
                                           [back_btn("kind")])
    if step == "fmt":
        return texts.DESIGN_FORMAT_STEP, ik([btn(f, "fmt", fmt_code(f)) for f in dsvc.FORMATS], [back_btn("style")])
    if step == "lang":
        return (texts.DESIGN_LANG_STEP.format(fmt=spec.get("fmt") or ""),
                ik([btn(label, "lang", code) for code, label in dsvc.LANG_BUTTONS.items()], [back_btn("fmt")]))
    if step == "qty":
        nums = [btn(str(n), "qty", str(n)) for n in dsvc.QTY_BUTTONS]
        return (texts.DESIGN_QTY_STEP.format(head=head(spec), price=fmt_rub(dsvc.PRICES[spec["kind"]])),
                ik(nums + [btn(texts.DESIGN_QTY_OTHER, "qty", "other")], [back_btn(prev_of("qty", spec))]))
    if step == "qty_input":
        return texts.DESIGN_QTY_PROMPT.format(max=dsvc.MAX_QTY), ik([back_btn("qty")])
    if step == "content":
        text = texts.DESIGN_CONTENT_STEP.format(head=head(spec, with_qty=True))
        what = sent_what(spec)
        if what:
            text += texts.DESIGN_SENT.format(what=what)
        return text, content_kb(bool(what))
    if step == "summary":
        return await summary_view(session, spec, user, s)
    if step == "terms":
        return (s.terms_text + "\n\n" + texts.TERMS_NEED,
                ik([btn(texts.TERMS_ACCEPT_BTN, "terms")], [back_btn("summary")]))
    raise ValueError(step)


def ready_for(step: str, spec: dict) -> str:
    """Шаг, до которого реально дошли (старые кнопки, рестарт): без типа - к выбору типа и т. д."""
    kind = spec.get("kind")
    if step == "kind":
        return step
    if kind not in dsvc.KINDS:
        return "kind"
    if kind == "pres":
        style = spec.get("style")
        if step != "style" and style not in dsvc.STYLES:
            return "style"
        if style == "ssau":
            if step not in ("style", "fmt") and spec.get("fmt") not in dsvc.FORMATS:
                return "fmt"
            if step not in ("style", "fmt", "lang") and spec.get("lang") not in dsvc.LANGS:
                return "lang"
        elif step in ("fmt", "lang"):
            return "style"
    elif step in ("style", "fmt", "lang"):
        return "qty"
    qty = spec.get("qty")
    if step in ("content", "summary", "terms") and not (isinstance(qty, int) and 1 <= qty <= dsvc.MAX_QTY):
        return "qty"
    if step in ("summary", "terms") and not sent_what(spec):
        return "content"
    return step


async def goto(step: str, target: CallbackQuery | Message, state: FSMContext, session: AsyncSession, spec: dict,
               user: User, s: RuntimeSettings, note: str | None = None) -> None:
    """Показать шаг: по кнопке - в том же сообщении, после сообщения клиента - новым."""
    step = ready_for(step, spec)
    upl.cancel_ack(user.id, "design")
    await state.set_state(STEPS[step])
    await put_spec(state, spec)
    text, kb = await render(step, session, spec, user, s)
    if note:
        text = f"{note}\n\n{text}"
    if isinstance(target, CallbackQuery):
        if step in ("summary", "terms"):  # итог - новым сообщением, кнопки шага задания убираем
            await drop_kb(target)
            await target.message.answer(text, reply_markup=kb)
        else:
            await show(target, text, kb)
    else:
        await target.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ вход
@menu_router.message(F.text == M["design"])
async def menu_design(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                      s: RuntimeSettings) -> None:
    await state.clear()
    await goto("kind", message, state, session, {"origin": "menu"}, db_user, s)


@router.callback_query(DesignCb.filter(F.a == "open"))
async def dz_open(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                  db_user: User, s: RuntimeSettings) -> None:
    """С экрана «📝 Заказать» (выбор предмета) или из подсказок: раздел в том же сообщении."""
    await call.answer()
    await state.clear()
    await goto("kind", call, state, session, {"origin": callback_data.v or "menu"}, db_user, s)


@router.callback_query(DesignCb.filter(F.a == "back"))
async def dz_back(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                  db_user: User, s: RuntimeSettings, config: Config) -> None:
    spec = await get_spec(state)
    to = callback_data.v
    if to == "origin":
        await call.answer()
        await drop_kb(call)
        await state.clear()
        if spec.get("origin") == "order":  # обратно к выбору предмета «📝 Заказать»
            await wizard.start_wizard(call.message, state, session, db_user, s, config)
        else:
            await call.message.answer(texts.MENU_HINT, reply_markup=main_menu())
        return
    await call.answer()
    await goto(to if to in STEPS else "kind", call, state, session, spec, db_user, s)


# ------------------------------------------------------------------ тип, оформление, формат, язык, количество
@router.callback_query(DesignCb.filter(F.a == "kind"))
async def dz_kind(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                  db_user: User, s: RuntimeSettings) -> None:
    if callback_data.v not in dsvc.KINDS:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    spec = await get_spec(state) if (await state.get_state() or "").startswith("DesignSG:") else {"origin": "menu"}
    if spec.get("kind") != callback_data.v:
        for key in ("style", "fmt", "lang"):
            spec.pop(key, None)
    spec["kind"] = callback_data.v
    await goto("style" if callback_data.v == "pres" else "qty", call, state, session, spec, db_user, s)


@router.callback_query(DesignCb.filter(F.a == "style"))
async def dz_style(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                   db_user: User, s: RuntimeSettings) -> None:
    spec = await get_spec(state)
    if callback_data.v not in dsvc.STYLES:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    spec["style"] = callback_data.v
    if callback_data.v == "custom":
        spec.pop("fmt", None)
        spec.pop("lang", None)
    await goto("fmt" if callback_data.v == "ssau" else "qty", call, state, session, spec, db_user, s)


@router.callback_query(DesignCb.filter(F.a == "fmt"))
async def dz_fmt(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                 db_user: User, s: RuntimeSettings) -> None:
    spec = await get_spec(state)
    fmt = fmt_of(callback_data.v)
    if fmt not in dsvc.FORMATS:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    spec["fmt"] = fmt
    await goto("lang", call, state, session, spec, db_user, s)


@router.callback_query(DesignCb.filter(F.a == "lang"))
async def dz_lang(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                  db_user: User, s: RuntimeSettings) -> None:
    spec = await get_spec(state)
    if callback_data.v not in dsvc.LANGS:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    spec["lang"] = callback_data.v
    await goto("qty", call, state, session, spec, db_user, s)


@router.callback_query(DesignCb.filter(F.a == "qty"))
async def dz_qty(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                 db_user: User, s: RuntimeSettings) -> None:
    spec = await get_spec(state)
    await call.answer()
    if callback_data.v == "other":
        await goto("qty_input", call, state, session, spec, db_user, s)
        return
    try:
        qty = int(callback_data.v)
    except ValueError:
        qty = 0
    if not 1 <= qty <= dsvc.MAX_QTY:
        await goto("qty", call, state, session, spec, db_user, s)
        return
    spec["qty"] = qty
    await goto("content", call, state, session, spec, db_user, s)


@router.message(DesignSG.qty_input)
async def dz_qty_text(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                      s: RuntimeSettings) -> None:
    raw = (message.text or "").strip()
    qty = int(raw) if raw.isdigit() else 0
    if not 1 <= qty <= dsvc.MAX_QTY:
        await message.answer(texts.DESIGN_QTY_BAD.format(max=dsvc.MAX_QTY), reply_markup=ik([back_btn("qty")]))
        return
    spec = await get_spec(state)
    spec["qty"] = qty
    await goto("content", message, state, session, spec, db_user, s)


# ------------------------------------------------------------------ задание: текст, фото, файлы
def _add_file(spec: dict, f: upl.IncomingFile) -> bool:
    """False - лимит файлов (файл не взяли). Повтор того же файла не добавляем."""
    files = spec.setdefault("files", [])
    if any(x.get("tg_file_unique_id") == f.unique_id for x in files):
        return True
    if len(files) >= dsvc.MAX_FILES:
        return False
    files.append(upl.file_fields(f, None))
    return True


def _add_text(spec: dict, text: str) -> bool:
    joined = f"{spec['text']}\n{text}" if spec.get("text") else text
    if len(joined) > dsvc.MAX_TEXT:
        return False
    spec["text"] = joined
    return True


@router.message(DesignSG.content, F.photo | F.document)
async def dz_file(message: Message, state: FSMContext, bot: Bot, db_user: User) -> None:
    """Фото или файл любого типа (до 20 МБ). Подпись - в текст задания. Альбом: одно подтверждение."""
    f = upl.any_file(message)
    if f is None:
        await message.answer(texts.STEP_FILE_BIG)
        return
    spec = await get_spec(state)
    if not _add_file(spec, f):
        _dropped.add(db_user.id)
    caption = (message.caption or "").strip()
    if caption:
        _add_text(spec, caption[:dsvc.MAX_TEXT])
    await put_spec(state, spec)
    raw, chat_id, user_id = await state.get_state(), message.chat.id, db_user.id

    async def ack() -> None:
        if await state.get_state() != raw:
            return
        what = sent_what(await get_spec(state))
        text = texts.DESIGN_ACK.format(what=what)
        if user_id in _dropped:
            _dropped.discard(user_id)
            text = texts.DESIGN_FILES_LIMIT.format(n=dsvc.MAX_FILES) + "\n" + text
        await bot.send_message(chat_id, text, reply_markup=content_kb(True))

    upl.schedule_ack(user_id, "design", ack)


@router.message(DesignSG.content, F.text)
async def dz_text(message: Message, state: FSMContext) -> None:
    t = (message.text or "").strip()
    if not t or t.startswith("/"):
        await message.answer(texts.DESIGN_BAD_CONTENT)
        return
    spec = await get_spec(state)
    if not _add_text(spec, t):
        await message.answer(texts.DESIGN_TEXT_LONG.format(n=dsvc.MAX_TEXT), reply_markup=content_kb(bool(sent_what(spec))))
        return
    await put_spec(state, spec)
    await message.answer(texts.DESIGN_ACK.format(what=sent_what(spec)), reply_markup=content_kb(True))


@router.message(DesignSG.content)
async def dz_other(message: Message, state: FSMContext) -> None:
    """Голосовые, стикеры, видео: просим текст, фото или файл."""
    await message.answer(texts.DESIGN_BAD_CONTENT, reply_markup=content_kb(bool(sent_what(await get_spec(state)))))


@router.callback_query(DesignSG.content, DesignCb.filter(F.a == "clear"))
async def dz_clear(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                   s: RuntimeSettings) -> None:
    spec = await get_spec(state)
    spec.pop("files", None)
    spec.pop("text", None)
    await call.answer("Очистили 🗑")
    await drop_kb(call)
    await put_spec(state, spec)
    await goto("content", call.message, state, session, spec, db_user, s)


@router.callback_query(DesignSG.content, DesignCb.filter(F.a == "done"))
async def dz_done(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                  s: RuntimeSettings) -> None:
    spec = await get_spec(state)
    if not sent_what(spec):
        await call.answer(texts.DESIGN_NEED_CONTENT, show_alert=True)
        return
    await call.answer()
    await goto("summary", call, state, session, spec, db_user, s)


@router.message(PICK_STATES, F.photo | F.document)
async def dz_file_early(message: Message, state: FSMContext, db_user: User) -> None:
    """Файл прислали раньше шага задания: сохраняем в задание, просим выбрать на кнопках."""
    f = upl.any_file(message)
    if f is None:
        await message.answer(texts.STEP_FILE_BIG)
        return
    spec = await get_spec(state)
    _add_file(spec, f)
    if (message.caption or "").strip():
        _add_text(spec, message.caption.strip()[:dsvc.MAX_TEXT])
    await put_spec(state, spec)
    chat_id, bot = message.chat.id, message.bot

    async def ack() -> None:
        await bot.send_message(chat_id, texts.DESIGN_EARLY_FILE)

    upl.schedule_ack(db_user.id, "design", ack)


@router.message(PICK_STATES)
async def dz_press_button(message: Message) -> None:
    await message.answer(texts.DESIGN_PRESS_BUTTON)


# ------------------------------------------------------------------ итог: оплатить, изменить, отмена
@router.callback_query(DesignCb.filter(F.a == "edit"))
async def dz_edit(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                  s: RuntimeSettings) -> None:
    if not (await state.get_state() or "").startswith("DesignSG:"):
        await call.answer(texts.DESIGN_LOST, show_alert=True)
        await drop_kb(call)
        return
    await call.answer()
    await goto("kind", call, state, session, await get_spec(state), db_user, s, note=texts.DESIGN_EDIT_NOTE)


@router.callback_query(DesignCb.filter(F.a == "cancel"))
async def dz_cancel(call: CallbackQuery, state: FSMContext, db_user: User) -> None:
    upl.cancel_ack(db_user.id, "design")
    if (await state.get_state() or "").startswith("DesignSG:"):
        await state.clear()
    await call.answer()
    await drop_kb(call)
    await call.message.answer(texts.DESIGN_CANCELLED, reply_markup=main_menu())


@router.callback_query(DesignSG.summary, DesignCb.filter(F.a == "pay"))
async def dz_pay(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                 s: RuntimeSettings, bot: Bot, config: Config) -> None:
    spec = await get_spec(state)
    step = ready_for("summary", spec)
    if step != "summary":
        await call.answer()
        await goto(step, call, state, session, spec, db_user, s)
        return
    await call.answer()
    await drop_kb(call)
    if db_user.terms_accepted_at is None:
        await goto("terms", call.message, state, session, spec, db_user, s)
        return
    await create_and_pay(call.message, state, session, db_user, s, bot, config, spec)


@router.callback_query(DesignSG.terms, DesignCb.filter(F.a == "terms"))
async def dz_terms(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                   s: RuntimeSettings, bot: Bot, config: Config) -> None:
    if db_user.terms_accepted_at is None:
        db_user.terms_accepted_at = now_utc()
        await session.commit()
    await call.answer("Принято ✅")
    await drop_kb(call)
    await call.message.answer(texts.TERMS_ACCEPTED.format(date=fmt_dt(db_user.terms_accepted_at)))
    await create_and_pay(call.message, state, session, db_user, s, bot, config, await get_spec(state))


@router.callback_query(DesignCb.filter(F.a.in_({"pay", "terms", "done", "clear"})))
async def dz_stale(call: CallbackQuery, state: FSMContext) -> None:
    """Кнопка с прошлого экрана (заказ уже создан или отменён)."""
    await call.answer(texts.DESIGN_LOST, show_alert=True)
    await drop_kb(call)


async def create_and_pay(message: Message, state: FSMContext, session: AsyncSession, user: User,
                         s: RuntimeSettings, bot: Bot, config: Config, spec: dict) -> None:
    """Создать заказ (ждёт оплаты) и сразу показать оплату. По безлимиту - бесплатно и сразу исполнителям."""
    now = now_utc()
    try:
        order = await dsvc.create_order(session, user.id, spec, list(spec.get("files") or []), spec.get("text"), now)
    except svc.OrderError as e:
        await message.answer(html.escape(str(e)))
        await goto(ready_for("summary", spec), message, state, session, spec, user, s)
        return
    await state.clear()
    actionlog.order_created(user, order)
    if order.status == "paid":  # безлимит: 0 ₽, оплачивать не нужно
        await message.answer(texts.DESIGN_UNLIMITED.format(id=order.id, what=dsvc.describe(order)),
                             reply_markup=main_menu())
        await designs.notify_paid(bot, session, config, order, user, autosend_files=bool(s.admin_autosend_files),
                                  tell_user=False)
    else:
        await message.answer(texts.DESIGN_CREATED.format(id=order.id, what=dsvc.describe(order),
                                                         price=fmt_rub(order.price_final or 0)),
                             reply_markup=main_menu())
        try:
            payment = await paysvc.start_order_payment(session, order.id, user.id, s, now_utc())
        except svc.OrderError as e:
            await message.answer(html.escape(str(e)))
        else:
            await pay.show_payment(message, session, payment, s, bot)
    await _keep_local_copies(bot, session, config, order.id)


async def _keep_local_copies(bot: Bot, session: AsyncSession, config: Config, order_id: int) -> None:
    """Локальные копии файлов задания (как у обычных заказов): если file_id перестанет работать."""
    for f in await svc.files_of(session, order_id, "condition"):
        inc = upl.IncomingFile(f.file_type, f.tg_file_id, f.tg_file_unique_id or f.tg_file_id, f.file_name,
                               f.mime_type, f.size)
        local = await upl.download(bot, inc, config.uploads_dir / str(order_id) / f"condition-{inc.unique_id}{inc.ext}")
        if local is not None:
            row = await session.get(OrderFile, f.id)
            if row is not None:
                row.local_path = str(local)
    await session.commit()


# ------------------------------------------------------------------ результат: «✅ Принять» / «🔁 Нужны правки»
async def _notify_assignee(bot: Bot, config: Config, assignee_id: int | None, text: str, order_id: int) -> None:
    only = {assignee_id} if assignee_id and assignee_id in config.staff else None
    await notify_admins(bot, config, text, only=only, markup_for=lambda _lvl: designs.admin_kb(order_id))


@router.callback_query(DesignCb.filter(F.a == "accept"))
async def dz_accept(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                    db_user: User, bot: Bot, config: Config) -> None:
    try:
        order = await svc.accept(session, callback_data.id, db_user.id, now_utc())
    except svc.OrderError as e:
        await call.answer(str(e), show_alert=True)
        return
    await call.answer()
    await drop_kb(call)
    if await state.get_state() == DesignSG.revision.state:
        await state.clear()
    await call.message.answer(texts.DESIGN_ACCEPTED.format(id=order.id), reply_markup=main_menu())
    actionlog.action(f"принял заказ #{order.id} ({dsvc.describe_plain(order)})", db_user)
    await mark_headers(bot, session, config, "order", order.id,
                       texts.DESIGN_ACCEPT_MARK.format(time=f"{to_local(now_utc()):%H:%M}"),
                       markup_for=lambda _lvl: designs.admin_kb(order.id))
    await _notify_assignee(bot, config, order.assignee_id, texts.ADMIN_DESIGN_ACCEPTED.format(
        id=order.id, what=dsvc.describe(order), user=display_name(db_user)), order.id)


@router.callback_query(DesignCb.filter(F.a == "redo"))
async def dz_redo(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                  db_user: User) -> None:
    order = await svc.get_order(session, callback_data.id)
    if order is None or order.user_id != db_user.id or not dsvc.is_design(order) or order.status != "done":
        await call.answer(STATUS_CHANGED, show_alert=True)
        return
    await call.answer()
    await state.set_state(DesignSG.revision)
    await state.update_data(rev=order.id)
    await call.message.answer(texts.DESIGN_REDO_PROMPT.format(id=order.id),
                              reply_markup=ik([btn(texts.BTN_CANCEL, "redo_end", id=order.id)]))


@router.message(DesignSG.revision)
async def dz_revision(message: Message, state: FSMContext, session: AsyncSession, db_user: User, bot: Bot,
                      config: Config) -> None:
    """Правки: каждое сообщение - в переписку заказа и исполнителю (если его нет среди админов - всем)."""
    order = await svc.get_order(session, int((await state.get_data()).get("rev") or 0))
    if order is None or order.user_id != db_user.id or order.status not in ("done", "in_work"):
        await state.clear()
        await message.answer(STATUS_CHANGED, reply_markup=main_menu())
        return
    body = message_text(message) or ""
    if not copyable(message) or (content_type(message) == "text" and not body.strip()) \
            or (message.text or "").startswith("/"):
        await message.answer(texts.DESIGN_BAD_CONTENT)
        return
    if len(body) > MAX_TEXT:
        await message.answer(f"Слишком длинно: уложись в {MAX_TEXT} символов или разбей на части.")
        return
    first = order.status == "done"
    if first:
        try:
            order = await dsvc.request_changes(session, order.id, db_user.id, now_utc())
        except svc.OrderError as e:
            await state.clear()
            await message.answer(str(e), reply_markup=main_menu())
            return
    only = {order.assignee_id} if order.assignee_id and order.assignee_id in config.staff else None
    await deliver(message, session, db_user, bot, config, "order", order.id,
                  note=texts.DESIGN_REDO_NOTE.format(id=order.id, what=dsvc.describe(order)), only=only)
    if first:
        actionlog.action(f"попросил правки по заказу #{order.id} ({dsvc.describe_plain(order)})", db_user)
        await mark_headers(bot, session, config, "order", order.id,
                           texts.DESIGN_REDO_MARK.format(time=f"{to_local(now_utc()):%H:%M}"),
                           markup_for=lambda _lvl, oid=order.id: designs.admin_kb(oid))
    chat_id, order_id = message.chat.id, order.id

    async def ack() -> None:
        await bot.send_message(chat_id, texts.DESIGN_REDO_ACK,
                               reply_markup=ik([btn(texts.DESIGN_REDO_DONE_BTN, "redo_end", id=order_id)]))

    upl.schedule_ack(db_user.id, "design_redo", ack)


@router.callback_query(DesignCb.filter(F.a == "redo_end"))
async def dz_redo_end(call: CallbackQuery, callback_data: DesignCb, state: FSMContext, session: AsyncSession,
                      db_user: User) -> None:
    upl.cancel_ack(db_user.id, "design_redo")
    if await state.get_state() == DesignSG.revision.state:
        await state.clear()
    await call.answer()
    await drop_kb(call)
    order = await svc.get_order(session, callback_data.id)
    sent = order is not None and order.user_id == db_user.id and order.status == "in_work"
    await call.message.answer(texts.DESIGN_REDO_WAIT if sent else texts.CANCELLED, reply_markup=main_menu())
