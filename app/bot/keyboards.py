from __future__ import annotations

from contextvars import ContextVar

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from app import texts
from app.bot.callbacks import AdminCb
from app.config import Config

Btn = InlineKeyboardButton

# Кому сейчас отвечаем: админу — показываем в нижнем меню «🛡 Админка». Ставит UserMiddleware на каждый апдейт.
# Сообщения ДРУГИМ людям (уведомления клиентам из админки) должны передавать admin=False явно.
_menu_admin: ContextVar[bool] = ContextVar("aegis_menu_admin", default=False)


# Черновик заказа того, кому отвечаем: число фото условия (None — черновика нет). Ставит UserMiddleware,
# обновляют обработчики, которые меняют черновик. Нижнее меню показывает «📝 Продолжить оформление заказа».
_menu_draft: ContextVar[int | None] = ContextVar("aegis_menu_draft", default=None)


def set_menu_admin(flag: bool) -> None:
    _menu_admin.set(flag)


def set_menu_draft(photos: int | None) -> None:
    _menu_draft.set(photos)


def menu_draft() -> int | None:
    return _menu_draft.get()


def ik(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


def cb(text: str, data) -> InlineKeyboardButton:  # noqa: ANN001
    return Btn(text=text, callback_data=data.pack() if hasattr(data, "pack") else data)


def main_menu(admin: bool | None = None, draft: int | None = None) -> ReplyKeyboardMarkup:
    """Нижнее меню. Сверху — «📝 Продолжить оформление заказа (фото: N)», если есть черновик, дальше главное
    действие, у админов — ещё кнопка «🛡 Админка».

    admin=None — меню для того, кому сейчас отвечаем (роль и черновик — из контекста апдейта).
    Явный admin=True/False — меню для другого человека: черновик показываем, только если передан draft."""
    own = admin is None
    if own:
        admin = _menu_admin.get()
        if draft is None:
            draft = _menu_draft.get()
    m = texts.MAIN_MENU
    rows = [[m["order"], m["explain"]], [m["my_orders"], m["balance"]], [m["invite"], m["gifts"]],
            [m["promo"], m["help"]]]
    if draft is not None:
        rows.insert(0, [texts.DRAFT_MENU_BTN.format(n=draft)])
    if admin:
        rows[-1].append(texts.ADMIN_MENU_BTN)
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Выбери пункт меню 👇")


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=texts.PHONE_BTN, request_contact=True)],
                                         [KeyboardButton(text=texts.BTN_BACK)]],
                               resize_keyboard=True, one_time_keyboard=True)


def no_menu() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def solution_button(config: Config, token: str | None, fallback) -> InlineKeyboardButton:  # noqa: ANN001
    if config.webapp_url and token:
        return Btn(text=texts.OPEN_SOLUTION, web_app=WebAppInfo(url=f"{config.webapp_url}/s/{token}"))
    return cb(texts.OPEN_SOLUTION, fallback)


def pager(page: int, total: int, per_page: int, make) -> list[InlineKeyboardButton] | None:  # noqa: ANN001
    """Ряд «⬅️ 2/5 ➡️». make(page) → CallbackData."""
    pages = max((total + per_page - 1) // per_page, 1)
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(cb("⬅️", make(page - 1)))
    row.append(cb(f"{page + 1}/{pages}", make(page)))
    if page < pages - 1:
        row.append(cb("➡️", make(page + 1)))
    return row


def order_alert_kb(order_id: int) -> InlineKeyboardMarkup:
    """Кнопки под уведомлением админам о заказе. «✉️ Ответить» — режим чата с клиентом по заказу."""
    return ik([cb("📦 Открыть заказ", AdminCb(s="o", a="open", id=order_id)),
               cb("🖼 Файлы", AdminCb(s="o", a="files", id=order_id))],
              [cb(texts.CHAT_BTN, AdminCb(s="c", a="o", id=order_id))])
