"""Сборка и запуск в одном процессе: бот (long polling) + веб-сервер Mini App + планировщик."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import __version__
from app.bot.commands import set_commands
from app.bot import pinned as pinned_glue
from app.bot.handlers import (
    admin,
    banned,
    channel,
    common,
    design,
    errors,
    my_orders,
    order,
    payments,
    promo,
    promocodes,
    stray,
    support,
)
from app.bot.middlewares import (
    ActionLogMiddleware,
    AntiFloodMiddleware,
    DbSessionMiddleware,
    UserLockMiddleware,
    UserMiddleware,
)
from app.bot.storage import DBStorage
from app.config import Config
from app.db.engine import make_engine, make_sessionmaker
from app.scheduler import burn_bonuses, setup_scheduler
from app.services import channel as chsvc
from app.services import owner, paymode, roles, settings
from app.utils.timeutil import now_utc
from app.web.server import start_web

log = logging.getLogger(__name__)


def _detached(router: Router) -> Router:
    """Роутеры - модульные объекты; отцепляем от старого диспетчера (повторная сборка в тестах)."""
    parent = router.parent_router
    if parent is not None:
        parent.sub_routers.remove(router)
        router._parent_router = None  # noqa: SLF001
    return router


def build_dispatcher(config: Config, sessionmaker: async_sessionmaker[AsyncSession], bot_username: str) -> Dispatcher:
    dp = Dispatcher(storage=DBStorage(sessionmaker))
    dp["config"] = config
    dp["sessionmaker"] = sessionmaker
    dp["bot_username"] = bot_username
    dp["owner_claim"] = owner.OwnerClaim(bot_username)
    dp.update.outer_middleware(AntiFloodMiddleware(config))
    dp.update.outer_middleware(UserLockMiddleware())  # v2.4: апдейты одного пользователя по очереди
    dp.update.outer_middleware(ActionLogMiddleware())  # v2.4: журнал действий aegis.actions
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.update.outer_middleware(UserMiddleware(config))
    # порядок важен: ошибки → бан → кнопки меню и команды (из любого состояния) → админка (и её режим чата) →
    # свайп-ответы клиента команде (из любого шага) → промокод → сценарии → «Это задание?» → фолбэк
    for router in (errors.router, banned.router, common.menu_router, admin.router, support.reply_router,
                   promocodes.router, promo.router, order.router, design.router, my_orders.router, payments.router,
                   support.router,
                   stray.router, channel.router, common.router, common.fallback_router):
        dp.include_router(_detached(router))
    return dp


def make_bot(config: Config) -> Bot:
    session = None
    if config.telegram_test_env:
        session = AiohttpSession(api=TelegramAPIServer(
            base="https://api.telegram.org/bot{token}/test/{method}",
            file="https://api.telegram.org/file/bot{token}/test/{path}",
        ))
    return Bot(config.bot_token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


async def check_channel(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Бонусы за подписку работают, только если бот - админ канала: предупреждаем сразу при запуске."""
    try:
        async with sessionmaker() as session:
            channel = (await settings.load_settings(session)).channel
        problem = await chsvc.bot_rights_problem(bot, channel, now_utc())
    except Exception:  # noqa: BLE001 - проверка не должна мешать запуску
        log.exception("не удалось проверить права бота в канале")
        return
    if problem:
        log.warning("⚠️ %s: добавьте бота администратором канала (достаточно минимальных прав), иначе "
                    "подписка не проверяется и бонусы за неё не начисляются", problem)
    elif channel:
        log.info("канал %s: бот, администратор, подписки проверяются", channel)


async def run(config: Config) -> None:
    settings.configure(config)
    engine = make_engine(config.database_url)
    sessionmaker = make_sessionmaker(engine)
    bot = make_bot(config)
    runner = scheduler = None
    try:
        me = await bot.get_me()
        username = me.username or config.bot_username.lstrip("@")
        if config.bot_username and config.bot_username.lstrip("@").lower() != username.lower():
            log.warning("BOT_USERNAME=%s, а токен от @%s: используется @%s", config.bot_username, username, username)
        dp = build_dispatcher(config, sessionmaker, username)
        async with sessionmaker() as session:
            if await owner.restore_saved(session, config):
                log.info("ADMIN_IDS пуст: админ из базы (назначен кодом): %s", owner.ids_text(config.admin_ids))
            await roles.load(session, config)
        await pinned_glue.startup_sync(bot, config, sessionmaker)  # 📌 SENIOR_ADMIN_* из .env ↔ база
        log.info("админов в команде: %s (владельцы: %s)", len(config.staff), owner.ids_text(config.admin_ids) or "-")
        await bot.delete_webhook(drop_pending_updates=False)
        await set_commands(bot, config)
        await check_channel(bot, sessionmaker)
        try:
            runner = await start_web(config, sessionmaker, username)
        except OSError as e:  # без домена Mini App всё равно выключены - это не авария
            (log.error if config.webapp_url else log.warning)(
                "веб-сервер не запустился на %s:%s (%s), Mini App недоступны", config.web_host, config.web_port, e)
        scheduler = setup_scheduler(bot, config, sessionmaker)
        scheduler.start()
        try:
            await burn_bonuses(bot, sessionmaker)  # если бот лежал в 00:05
        except Exception:  # noqa: BLE001
            log.exception("сгорание бонусов при старте не удалось")
        async with sessionmaker() as session:
            mode = (await settings.load_settings(session)).payments_mode
            removed = await paymode.tidy_on_start(session, mode, now_utc())
        if removed.money:
            log.warning("оплата %s: убраны остатки теста, тестовых счетов %s, рублей %s коп., бонусов %s коп.",
                        mode, removed.invoices, removed.rub, removed.bonus)
        log.info("Аегис %s запущен: @%s | оплата: %s | Mini App: %s | админы: %s", __version__, username, mode,
                 config.webapp_url or "выкл (решения уходят HTML-файлом)",
                 owner.ids_text(config.admin_ids) or "не назначены")
        if not config.admin_ids:
            log.warning(owner.banner(dp["owner_claim"].start(), username))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        if runner is not None:
            await runner.cleanup()
        await bot.session.close()
        await engine.dispose()
        log.info("Аегис остановлен")
