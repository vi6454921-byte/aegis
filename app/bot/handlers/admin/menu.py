"""/admin: панель с тем, что требует внимания, блок «🔐 Управление (2 уровень)», статистика, справка по админке.

v2.3: «📥 Входящие» (заказы на проверке, споры и обращения без ответа - одним списком) и, если админ в режиме
чата с клиентом, напоминание и «↩️ Вернуться в чат по заказу №…»."""
from __future__ import annotations

import html

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot import conversation
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin import inbox, manage
from app.bot.handlers.admin.base import L2, admin_router, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.ui import show
from app.config import Config
from app.db.models import Payment, User
from app.services import channel as chsvc
from app.services import orders as svc
from app.services import promo, roles, slots, stats, tickets
from app.services.settings import ENUM_LABELS, RuntimeSettings
from app.utils.timeutil import fmt_left, now_utc

router = admin_router("admin_menu")


async def pending_receipts(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count(Payment.id)).where(
        Payment.provider == "manual", Payment.claimed_at.is_not(None), Payment.reviewed_at.is_(None),
        Payment.status.in_(("pending", "cancelled")))) or 0)


async def menu_view(session: AsyncSession, s: RuntimeSettings, level: int, admin_id: int | None = None
                    ) -> tuple[str, InlineKeyboardMarkup]:
    now = now_utc()
    chat = await conversation.get_chat(session, admin_id) if admin_id else None
    n_inbox = await inbox.count(session)
    c = await svc.status_counts(session)
    n_tickets = await tickets.open_count(session)
    n_receipts = await pending_receipts(session)
    n_tasks = await promo.pending_total(session)
    active = sum(c.get(k, 0) for k in svc.ACTIVE)
    lines = [texts.ADMIN_TITLE + f" · {roles.LEVEL_SHORT.get(level, '')}"]
    if chat is not None:
        mark = conversation.Target.of(chat).mark
        lines.append("\n" + texts.CHAT_REMINDER.format(
            user=conversation.user_label(await session.get(User, chat.user_id)), mark=mark))
    todo = []
    if n_inbox:
        todo.append(texts.INBOX_TODO.format(n=f"<b>{n_inbox}</b>"))
    if c.get("estimate"):
        todo.append(f"📎 Оценить задачу и назвать цену: <b>{c['estimate']}</b>")
    if c.get("review"):
        todo.append(f"🔎 Проверить и выставить счёт: <b>{c['review']}</b>")
    if n_receipts:
        todo.append(f"🧾 Подтвердить оплату переводом: <b>{n_receipts}</b>")
    if c.get("paid"):
        todo.append(f"✅ Оплачены, ждут исполнителя: <b>{c['paid']}</b>")
    if c.get("dispute"):
        todo.append(f"⚠️ Споры «не зачли»: <b>{c['dispute']}</b>")
    if n_tickets:
        todo.append(f"📨 Сообщения без ответа: <b>{n_tickets}</b>")
    if n_tasks:
        todo.append(texts.TASKS_ATTENTION.format(n=f"<b>{n_tasks}</b>"))
    lines.append("\n<b>Требует внимания</b>")
    lines += todo or ["Всё разобрано 👌"]
    nearest = await svc.nearest_deadline(session, now)
    lines.append("")
    lines.append(f"🛠 В работе: {c.get('in_work', 0)} · 💳 Ждут оплаты: {c.get('awaiting_payment', 0)}")
    if nearest is not None and nearest.deadline_at is not None:
        lines.append(f"⏰ Ближайший дедлайн: #{nearest.id}, {fmt_left(nearest.deadline_at, now)}")
    intake = await slots.intake_state(session, s, now)
    if intake.reason == "manual":
        intake_line = "закрыт вручную"
    elif intake.remaining is None:
        intake_line = f"открыт вручную, без лимита (сегодня задач: {intake.used})"
    elif intake.open:
        intake_line = f"открыт · сегодня {intake.used}/{intake.limit} задач"
    else:
        intake_line = f"закрыт: лимит {intake.limit} задач на сегодня исчерпан"
    lines.append(f"📦 Приём: {intake_line}")
    lines.append(f"💳 Оплата: {ENUM_LABELS['payments_mode'].get(s.payments_mode, s.payments_mode)}")
    warnings = []
    if s.payments_mode == "manual" and not str(s.manual_pay_details or "").strip():
        warnings.append("⚠️ Не указаны реквизиты для перевода: «⚙️ Настройки» → «💳 Оплата».")
    if s.channel and chsvc.failing():
        warnings.append(f"⚠️ Не проверяется подписка на {html.escape(s.channel)}: сделайте бота админом канала.")
    if warnings:
        lines.append("")
        lines += warnings
    rows = [[conversation.return_btn(chat)]] if chat is not None else []
    rows += [[cb(texts.INBOX_BTN.format(n=n_inbox) if n_inbox else texts.INBOX_BTN.split(" ·")[0], AdminCb(s="in"))],
             [cb(f"📥 Заказы · {active}", AdminCb(s="o", a="list", v="q")),
              cb(f"📨 Обращения · {n_tickets}" if n_tickets else "📨 Обращения", AdminCb(s="t"))]]
    users_btn = cb("👥 Пользователи", AdminCb(s="u", a="list"))
    tasks_btn = cb(f"{texts.TASKS_BTN} · {n_tasks}" if n_tasks else texts.TASKS_BTN, AdminCb(s="pt"))
    if s.payments_mode == "manual" or n_receipts:
        rows += [[cb(f"🧾 Чеки · {n_receipts}" if n_receipts else "🧾 Чеки", AdminCb(s="pay")), tasks_btn],
                 [users_btn]]
    else:
        rows.append([users_btn, tasks_btn])
    if level >= 2 and s.payments_mode == "test":  # главный шаг к запуску - на виду; переключатель с подтверждением
        rows.append([cb("⭐ Включить оплату звёздами", AdminCb(s="s", a="pm_ask", v="stars"))])
    if level >= 2:  # блок «🔐 Управление (2 уровень)»: только старшим - у 1 уровня этих кнопок нет совсем
        rows.append([cb(texts.MANAGE_BTN, AdminCb(s="g"))])
        rows += manage.hub_rows()
    rows.append([cb("❓ Справка", AdminCb(s="m", a="help"))])
    rows.append([cb("🔄 Обновить", AdminCb(s="m", a="refresh"))])
    return "\n".join(lines), ik(*rows)


HELP_L1 = (
    "❓ <b>Как работать в админке</b>\n\n"
    "<b>📥 Заказы.</b> Новый заказ приходит уведомлением с фото. Открой карточку → проверь условие "
    "(и фото конспектов: ✅/❌) → при необходимости поправь количество задач → «💳 Выставить счёт».\n"
    "После оплаты: «🛠 Взять в работу» → «📤 Загрузить решение» (HTML/ZIP) → «📦 Готово, отправить клиенту».\n\n"
    "<b>♾ Безлимит (промокод).</b> Заказ клиента с безлимитом помечен ♾: счёт не нужен, проверь фото и нажми "
    "«✅ Принять в очередь (безлимит, 0 ₽)». Оплата в карточке: «Промокод», 0 ₽.\n\n"
    "<b>📥 Входящие</b>: всё, что ждёт нас, одним списком: заказы на проверке, споры и обращения без ответа "
    "(сначала новые, есть фильтр «за сегодня»). Заказ открывается вместе с фото.\n\n"
    "<b>✉️ Ответить</b> (в карточке заказа, в обращении, под уведомлением о заказе) включает чат с клиентом: "
    "всё, что пришлёшь боту: текст, фото, альбомы, файлы, голосовые, видео, сразу уходит клиенту с пометкой "
    "«По заказу №…», а под твоим сообщением: «✅ Доставлено» (или почему не доставлено). Чат действует до "
    "«✅ Завершить» (или «📦 Готово, отправить клиенту»), даже после перезапуска бота; другие кнопки его не "
    "ломают, а в /admin есть «↩️ Вернуться в чат». Свайп-ответ на уведомление работает так же. Клиент отвечает "
    "свайпом или кнопкой: всё складывается в одну переписку, её видно в карточке заказа («💬 Переписка»).\n\n"
    "<b>📸 Объяснение материала.</b> После оплаты: «🛠 Взять в работу» → «✉️ Ответить», объясняй сообщениями. "
    "Когда всё объяснил: «📦 Готово, отправить клиенту».\n\n"
    "<b>📨 Сообщения клиентов</b> (поддержка, оплата, вопросы по заказу, обжалование бана) приходят сюда. "
    "Ответить: кнопка «✉️ Ответить» или просто свайп-ответ на уведомление, клиент получит ответ от имени бота. "
    "У остальных админов уведомление пометится «✅ Ответил(а)», чтобы не отвечать дважды.\n\n"
    "<b>🧾 Оплата переводом.</b> Клиент присылает чек: сверь сумму с поступлением и нажми "
    "«✅ Оплата пришла» или «❌ Не пришла».\n\n"
    "<b>🎯 Задания.</b> Заявки «📌 Ссылка в профиле» и «📣 Пост о нас» приходят карточкой со скрином. "
    "Профиль: проверь на скрине ссылку в «О себе» (автопроверка, только подсказка) → «✅ Начислить» или "
    "«❌ Отклонить» с причиной. Пост: «✅ Принять» или «❌ Отклонить»; отчёт о просмотрах, сверь число на скрине "
    "и подтверди (или впиши своё; меньше прошлого нельзя). Деньги: обычные рубли на баланс, у остальных админов "
    "карточка пометится, повторно не начислится. «🗑 Пост удалён»: новые отчёты по нему не принимаются.\n\n"
    "<b>Команды.</b> /user <code>@username</code> или <code>ID</code>: карточка пользователя; "
    "все доступные тебе команды: /commands.\n\n"
    "<b>Отмена.</b> Любой ввод отменяется кнопкой «✖️ Отмена»: вернёт на тот же экран."
)
HELP_L2 = (
    "\n\n<b>🔐 Управление (2 уровень)</b>: блок кнопок на главном экране админки (у 1 уровня его нет):\n"
    "• 👮 Админы: пригласить ссылкой, добавить по @username или ID, снять. Админов 2 уровня назначает и снимает "
    "только владелец; 📌 закреплённых в .env: тоже только он.\n"
    "• ⭐ Приоритет / 🎟 Месяц бесплатно: выдать или снять; для месяца можно выбрать дни и число задач.\n"
    "• 🚫 Бан / разбан: с причиной и подтверждением; админов и себя забанить нельзя.\n"
    "• 💸 Деньги: выдать или списать рубли (можно с копейками и комментарием). Сумма больше порога из "
    "настроек: с повторным подтверждением; списание не уводит баланс ниже нуля.\n"
    "• 🗄 Запросы к БД: файл базы, выгрузки Excel/CSV и готовые отчёты (только чтение, без SQL).\n"
    "• 🎟 Промокоды: коды на безлимит, их статусы (свободен / кем и когда активирован / отозван), выгрузка в txt, "
    "отзыв (клиенту придёт уведомление), выпуск новых, ручная выдача и снятие безлимита.\n"
    "• 📊 Статистика, 📣 Рассылка, ⚙️ Настройки (цены, наценки, канал, задания, режим оплаты, тексты, подарки), "
    "💾 Данные (бэкап, очистка теста, проверка целостности).\n"
    "В карточке пользователя: балансы и бонусы, ⭐/🎟, бан, ручная цена и возвраты, в заказах.\n\n"
    "<b>Команды 2 уровня:</b> /give <code>@username 500</code> (или просто сообщение <code>@username +500</code>, "
    "<code>-500</code>: списать), /ban, /unban, /priority, /freemonth, /addadmin, /removeadmin, /db, /backup. "
    "Без аргументов команда открывает нужный экран. Полный список: /commands."
)


@router.message(Command("admin"))
@router.message(F.text == texts.ADMIN_MENU_BTN)
async def cmd_admin(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings,
                    config: Config) -> None:
    await state.clear()
    text, kb = await menu_view(session, s, config.admin_level(message.from_user.id), message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(AdminCb.filter((F.s == "m") & (F.a == "stats")), L2)
async def cb_stats(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    text = await stats.admin_report(session, now_utc())
    await show(call, text, ik([cb("🔄 Обновить", AdminCb(s="m", a="stats")), menu_btn()]))


@router.callback_query(AdminCb.filter((F.s == "m") & (F.a == "help")))
async def cb_help(call: CallbackQuery, config: Config) -> None:
    await call.answer()
    text = HELP_L1 + (HELP_L2 if config.admin_level(call.from_user.id) >= 2 else "")
    await show(call, text, ik([menu_btn()]))


@router.callback_query(AdminCb.filter((F.s == "m") & (F.a != "stats")))
async def cb_menu(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                  s: RuntimeSettings, config: Config) -> None:
    await call.answer("Обновлено" if callback_data.a == "refresh" else None)
    await state.clear()
    text, kb = await menu_view(session, s, config.admin_level(call.from_user.id), call.from_user.id)
    await show(call, text, kb)
