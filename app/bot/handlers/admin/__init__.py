"""Админ-панель. Каждый подроутер сам фильтрует доступ по уровню (IsAdmin на message и callback_query):
1 уровень — заказы, обращения, входящие, чат с клиентом, чеки, задания, пользователи (просмотр и сообщения);
2 уровень — всё остальное, включая блок «🔐 Управление (2 уровень)» (там же «🎟 Промокоды»). Команды админов
(manage) — первыми: работают из любого шага ввода. Режим чата (chat) — последним: забирает только сообщения,
которые не нужны ни одному вводу и не являются свайп-ответом на уведомление."""
from aiogram import Router
from aiogram.types import CallbackQuery

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin import (
    broadcast,
    cancel,
    chat,
    data,
    dbq,
    inbox,
    manage,
    menu,
    orders,
    promocodes,
    receipts,
    settings,
    staff,
    tasks,
    tickets,
    users,
)
from app.config import Config

router = Router(name="admin")
denied = Router(name="admin_denied")


@denied.callback_query(AdminCb.filter())
async def admin_denied(call: CallbackQuery, config: Config) -> None:
    """Сюда попадают кнопки админки, которые не прошли проверку уровня (или устарели)."""
    level = config.admin_level(call.from_user.id) if call.from_user else 0
    if level <= 0:
        text = texts.ADMIN_ONLY
    elif level == 1:
        text = texts.ADMIN_L2_ONLY
    else:
        text = texts.STALE_BUTTON
    await call.answer(text, show_alert=True)


router.include_routers(manage.router, menu.router, inbox.router, promocodes.router, tickets.router, orders.router,
                       users.router, receipts.router, tasks.router, cancel.router, staff.router, broadcast.router,
                       settings.router, data.router, dbq.router, chat.router, denied)
