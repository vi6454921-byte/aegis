"""Закреплённые админы 2 уровня: уведомления владельцам и меню команд. База и правила — app/services/pinned.py."""
from __future__ import annotations

import html
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot.commands import refresh_commands
from app.bot.keyboards import main_menu
from app.bot.notify import safe_send, staff_targets
from app.config import Config
from app.db.models import User
from app.services import pinned as svc
from app.utils.timeutil import now_utc

log = logging.getLogger(__name__)


def who_of(user: User | None) -> str | None:
    """@ник или имя (экранировано) — id в сообщении пишется отдельно."""
    if user is None:
        return None
    return "@" + html.escape(user.username) if user.username else html.escape(user.first_name or "без имени")


def owner_targets(config: Config) -> list[int]:
    """Кому сообщать о закреплении: владельцам, а если их ещё нет — админам 2 уровня."""
    return sorted(config.admin_ids) or [uid for uid, _ in staff_targets(config, 2)]


async def tell_owners(bot: Bot, config: Config, text: str, exclude: int | None = None) -> None:
    for uid in owner_targets(config):
        if uid != exclude:
            await safe_send(bot, uid, text)


async def on_update_user(bot: Bot, session: AsyncSession, config: Config, tg_user) -> None:  # noqa: ANN001
    """Из UserMiddleware: человек с ником из SENIOR_ADMIN_USERNAMES написал боту."""
    res = await svc.on_user(session, config, tg_user, now_utc())
    if res is None:
        return
    if res.kind == "spoof":
        await tell_owners(bot, config, texts.PINNED_SPOOF.format(
            username=html.escape(res.username), bound=res.bound_id, id=res.user_id,
            name=html.escape(getattr(tg_user, "full_name", "") or "без имени")))
        return
    await tell_owners(bot, config, texts.PINNED_BOUND.format(who="@" + html.escape(res.username), id=res.user_id),
                      exclude=res.user_id)
    await refresh_commands(bot, config, res.user_id)
    await safe_send(bot, res.user_id, texts.PINNED_WELCOME, reply_markup=main_menu(True))


async def startup_sync(bot: Bot, config: Config, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """При запуске: сверить закреплённых с .env (кого убрали — снять, новые SENIOR_ADMIN_IDS — закрепить)."""
    async with sessionmaker() as session:
        res = await svc.sync(session, config, now_utc())
        names = {uid: who_of(await session.get(User, uid)) for uid in res.bound}
    for uid in res.bound:
        who = names.get(uid) or "по SENIOR_ADMIN_IDS"
        log.warning("📌 закреплён админ 2 уровня по SENIOR_ADMIN_IDS: %s", uid)
        await tell_owners(bot, config, texts.PINNED_BOUND.format(who=who, id=uid), exclude=uid)
    for who, uid, source in res.removed:
        log.warning("📌 закрепление снято (нет в %s): %s", source, who)
        await refresh_commands(bot, config, uid)
        await safe_send(bot, uid, texts.ADMIN_REMOVED_NOTICE, reply_markup=main_menu(False))
        await tell_owners(bot, config, texts.PINNED_UNPINNED.format(who=html.escape(who), source=source))
