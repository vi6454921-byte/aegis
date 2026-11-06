"""v2.4: «🎨 Файлы и дизайн»: презентации, PDF, картинки, баннеры и QR-коды по заданию клиента.

Цена фиксированная, без срочности и без оценки админом: презентация 50 ₽ за штуку (свой дизайн или шаблон
Самарского университета), PDF, картинка, баннер и QR-код 20 ₽ за штуку. Клиент выбирает тип (у презентации ещё
оформление, а у шаблона формат и язык), количество и присылает задание. На итоговом экране «💳 Оплатить» создаёт
заказ сразу в статусе «ждёт оплаты» (проверки условия нет). После оплаты заказ попадает во «📥 Входящие».

Админ присылает готовый файл через «✉️ Ответить», бот пересылает его клиенту и отмечает заказ готовым; клиент
нажимает «✅ Принять» или «🔁 Нужны правки» (правки уходят тому же админу, заказ снова в работе).

Шаблоны университета админ делает сам по https://ssau.ru/info/style/doc, бот только передаёт выбранные
формат и язык. Бот не связан с университетом и не говорит о себе как об официальном."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderFile, User
from app.services.admin_log import log_admin
from app.services.locks import app_lock
from app.services.orders import OrderError, get_order
from app.utils.money import fmt_rub

DESIGN = "design"
KINDS = {"pres": "📊 Презентация", "pdf": "📄 PDF-файл", "img": "🖼 Картинка", "banner": "🪧 Баннер",
         "qr": "🔳 QR-код"}
PRICES = {"pres": 5000, "pdf": 2000, "img": 2000, "banner": 2000, "qr": 2000}  # копейки за штуку
STYLES = {"custom": "🎨 Свой дизайн", "ssau": "🎓 Шаблон Самарского университета"}
FORMATS = ("16:9", "4:3")
LANGS = {"ru": "русский", "en": "английский"}
LANG_BUTTONS = {"ru": "Русский", "en": "English"}
QTY_BUTTONS = (1, 2, 3, 5)
MAX_QTY = 50
MAX_TEXT = 3000
MAX_FILES = 30
SSAU_URL = "https://ssau.ru/info/style/doc"
RESULT_TYPES = ("photo", "document")  # что админ присылает как готовый результат


def is_design(order: Order | None) -> bool:
    return order is not None and order.subject == DESIGN


def plain(label: str) -> str:
    """«📊 Презентация» → «Презентация» (для журнала и описания счёта)."""
    return label.split(" ", 1)[1] if " " in label else label


def valid_spec(spec: dict) -> bool:
    kind, qty = spec.get("kind"), spec.get("qty")
    if kind not in KINDS or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        return False
    if kind != "pres":
        return True
    style = spec.get("style")
    if style == "custom":
        return True
    return style == "ssau" and spec.get("fmt") in FORMATS and spec.get("lang") in LANGS


def style_text(style: str | None, fmt: str | None, lang: str | None) -> str | None:
    """«🎓 Шаблон Самарского университета, 16:9, русский» / «🎨 Свой дизайн» / None."""
    if style not in STYLES:
        return None
    if style == "ssau":
        return ", ".join(x for x in (STYLES[style], fmt, LANGS.get(lang or "")) if x)
    return STYLES[style]


def spec_style(spec: dict) -> str | None:
    return style_text(spec.get("style"), spec.get("fmt"), spec.get("lang")) if spec.get("kind") == "pres" else None


def order_style(order: Order) -> str | None:
    return style_text(order.design_style, order.design_format, order.design_lang) \
        if order.design_kind == "pres" else None


def total(kind: str, qty: int) -> int:
    return PRICES[kind] * qty


def price_line(kind: str, qty: int, price: int | None = None) -> str:
    """«🖼 Картинка × 3: <b>60 ₽</b>»."""
    return f"{KINDS.get(kind, kind)} × {qty}: <b>{fmt_rub(total(kind, qty) if price is None else price)}</b>"


def what(order: Order) -> str:
    """«🖼 Картинка × 3»."""
    return f"{KINDS.get(order.design_kind or '', order.design_kind or '?')} × {order.task_count or 1}"


def describe(order: Order) -> str:
    """«📊 Презентация × 2, 🎓 Шаблон Самарского университета, 16:9, русский»."""
    style = order_style(order)
    return what(order) + (f", {style}" if style else "")


def describe_plain(order: Order) -> str:
    """Для журнала и счёта: «Презентация × 2, Шаблон Самарского университета, 16:9, русский»."""
    style = order_style(order)
    return f"{plain(KINDS.get(order.design_kind or '', '?'))} × {order.task_count or 1}" + \
        (f", {plain(style)}" if style else "")


# ------------------------------------------------------------------ заказ
async def create_order(session: AsyncSession, user_id: int, spec: dict, files: list[dict], text: str | None,
                       now: datetime) -> Order:
    """«💳 Оплатить» на итоговом экране: заказ сразу ждёт оплаты по фиксированной цене.
    По безлимиту (промокод) заказ бесплатный и сразу оплачен. «Месяц бесплатно» тут не действует: это задачи."""
    if not valid_spec(spec):
        raise OrderError("Заказ заполнен не полностью: выбери тип и количество ещё раз.")
    text = (text or "").strip() or None
    if not files and not text:
        raise OrderError("Сначала пришли задание: текст, фото или файл.")
    kind, qty = spec["kind"], int(spec["qty"])
    pres = kind == "pres"
    async with app_lock():
        user = await session.get(User, user_id, populate_existing=True)
        if user is None:
            raise OrderError("Заказ не найден.")
        unlimited = user.unlimited_active(now)
        price = total(kind, qty)
        order = Order(user_id=user_id, subject=DESIGN, task_count=qty, comment=text,
                      design_kind=kind, design_style=spec.get("style") if pres else None,
                      design_format=spec.get("fmt") if pres and spec.get("style") == "ssau" else None,
                      design_lang=spec.get("lang") if pres and spec.get("style") == "ssau" else None,
                      price_per_task=PRICES[kind], urgency_pct=100, discount_pct=0, price_auto=price,
                      price_final=0 if unlimited else price, unlimited=unlimited,
                      lecture_status="none", practice_status="none",
                      status="paid" if unlimited else "awaiting_payment",
                      submitted_at=now, invoiced_at=now, paid_at=now if unlimited else None,
                      created_at=now, updated_at=now)
        session.add(order)
        await session.flush()
        for f in files[:MAX_FILES]:
            session.add(OrderFile(order_id=order.id, kind="condition", **f))
        await session.commit()
        return order


async def mark_delivered(session: AsyncSession, order_id: int, now: datetime, admin_id: int) -> tuple[Order, bool]:
    """Админ прислал клиенту файл по заказу: заказ готов. Возвращает (заказ, стал ли готовым сейчас)."""
    async with app_lock():
        order = await get_order(session, order_id)
        if order is None or not is_design(order):
            raise OrderError("Заказ не найден.")
        if order.status not in ("paid", "in_work"):
            return order, False
        if order.status == "paid":
            order.taken_at = now
        if order.assignee_id is None:
            order.assignee_id = admin_id
        order.status = "done"
        order.done_at = now
        log_admin(session, admin_id, "design_delivered", "order", order.id)
        await session.commit()
        return order, True


async def request_changes(session: AsyncSession, order_id: int, user_id: int, now: datetime) -> Order:
    """«🔁 Нужны правки»: готовый заказ снова в работе у того же исполнителя."""
    async with app_lock():
        order = await get_order(session, order_id)
        if order is None or order.user_id != user_id or not is_design(order):
            raise OrderError("Заказ не найден.")
        if order.status != "done":
            raise OrderError("Статус заказа уже изменился: открой его в «📦 Мои заказы».")
        order.status = "in_work"
        order.redo_count += 1
        await session.commit()
        return order
