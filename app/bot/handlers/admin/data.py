"""Админка → данные: бэкап сейчас, экспорт CSV, очистка тестовых данных, проверка целостности."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaDocument
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.base import admin_router, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.ui import show
from app.config import Config
from app.services import maintenance
from app.services.admin_log import log_admin
from app.services.backup import make_backup
from app.services.export import export_csv
from app.services.settings import RuntimeSettings
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)
router = admin_router("admin_data", level=2)
TG_UPLOAD_LIMIT = 49 * 1024 * 1024
TEST_LABELS = {"orders": "заказов", "payments": "платежей", "transactions": "транзакций", "lots": "бонусных партий"}


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "d") & (F.a == action))


def data_kb():  # noqa: ANN201
    return ik([cb("💾 Бэкап сейчас", AdminCb(s="d", a="backup")), cb("📤 Экспорт CSV", AdminCb(s="d", a="csv"))],
              [cb("📊 Пользователи в Excel", AdminCb(s="u", a="xlsx", v="d"))],
              [cb("🧹 Очистить тестовые данные", AdminCb(s="d", a="clear"))],
              [cb("🩺 Проверка целостности", AdminCb(s="d", a="check"))],
              [menu_btn()])


@router.callback_query(_f(""))
async def d_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, "💾 <b>Данные</b>\nВсё состояние бота лежит в папке data/: БД, бэкапы, решения, подарки, загрузки, "
                     "логи. Автобэкап: раз в сутки, хранятся 14 последних.\n"
                     "📊 «Пользователи в Excel»: таблица с колонками №, username, имя, телефон (если человек сам поделился), "
                     "что и когда покупал.", data_kb())


async def send_backup(bot: Bot, chat_id: int, session: AsyncSession, config: Config, admin_id: int) -> None:
    """Свежий бэкап БД (SQLite backup API) - файлом в чат; слишком большой остаётся на сервере. Пишется в журнал."""
    try:
        path = await make_backup(config)
    except Exception as e:  # noqa: BLE001
        log.exception("бэкап не удался")
        await bot.send_message(chat_id, f"⚠️ Бэкап не удался: {html.escape(str(e))}")
        return
    log_admin(session, admin_id, "backup_now", None, None, file=path.name)
    await session.commit()
    size = path.stat().st_size
    if size > TG_UPLOAD_LIMIT:
        await bot.send_message(chat_id, f"✅ Бэкап сохранён на сервере: data/backups/{path.name} "
                                        f"({size // 1024 // 1024} МБ: слишком большой для Telegram).")
        return
    await bot.send_document(chat_id, FSInputFile(path), caption=f"✅ Бэкап БД: {path.name}\n"
                                                              "Открыть: DB Browser for SQLite → Open Database.")


@router.callback_query(_f("backup"))
async def d_backup(call: CallbackQuery, session: AsyncSession, config: Config, bot: Bot) -> None:
    await call.answer("Делаю бэкап…")
    await send_backup(bot, call.from_user.id, session, config, call.from_user.id)


@router.callback_query(_f("csv"))
async def d_csv(call: CallbackQuery, session: AsyncSession, config: Config) -> None:
    await call.answer("Готовлю CSV…")
    paths = await export_csv(session, config.exports_dir)
    log_admin(session, call.from_user.id, "export_csv", None, None, files=[p.name for p in paths])
    await session.commit()
    caption = "📤 Экспорт: users, orders, transactions, payments (разделитель «;», UTF-8, открывается в Excel)"
    media = [InputMediaDocument(media=FSInputFile(p), caption=caption if i == len(paths) - 1 else None)
             for i, p in enumerate(paths)]
    await call.message.answer_media_group(media)


@router.callback_query(_f("clear"))
async def d_clear(call: CallbackQuery, session: AsyncSession) -> None:
    counts = await maintenance.test_data_counts(session)
    await call.answer()
    if not any(counts.values()):
        await show(call, "🧹 Тестовых данных нет.", data_kb())
        return
    lines = ["🧹 <b>Удалить тестовые данные?</b>"] + [f"• {TEST_LABELS.get(k, k)}: {v}" for k, v in counts.items()]
    lines.append("\nУдалятся тестовые заказы, платежи, движения денег и бонусы; балансы пересчитаются по журналу. "
                 "Реальные данные не трогаются.")
    await show(call, "\n".join(lines), ik([cb("🧹 Да, удалить", AdminCb(s="d", a="clear_go")),
                                           cb("✖️ Отмена", AdminCb(s="d"))]))


@router.callback_query(_f("clear_go"))
async def d_clear_go(call: CallbackQuery, session: AsyncSession, config: Config, s: RuntimeSettings) -> None:
    await call.answer("Чищу…")
    counts = await maintenance.clear_test_data(session, config, s, call.from_user.id, now_utc())
    issues = await maintenance.integrity_issues(session)
    text = "✅ Удалено: " + ", ".join(f"{TEST_LABELS.get(k, k)} {v}" for k, v in counts.items())
    text += "\n" + ("🩺 Целостность: всё сходится." if not issues else f"⚠️ Расхождений: {len(issues)}. Нажми «Проверка».")
    await show(call, text, data_kb())


@router.callback_query(_f("check"))
async def d_check(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    issues = await maintenance.integrity_issues(session)
    tx, rub, bonus = await maintenance.ledger_totals(session)
    if not issues:
        text = (f"🩺 <b>Всё сходится</b>\nТранзакций в журнале: {tx}\nНа балансах: рублями {fmt_rub(rub)}, "
                f"бонусами {fmt_rub(bonus)}\nСумма транзакций = кэш балансов, остатки партий = журнал.")
    else:
        text = f"⚠️ <b>Расхождения: {len(issues)}</b>\n" + "\n".join(html.escape(i) for i in issues[:30])
    await show(call, text, data_kb())
