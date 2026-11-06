"""Команды по ролям: меню команд Telegram (кнопка «Меню») и список /commands.

• Обычный пользователь - общий список (BotCommandScopeDefault).
• Админ 1 уровня, 2 уровня и владелец - каждый свой список в своём чате (BotCommandScopeChat).
• Меню обновляется при назначении, повышении, понижении, снятии админа и привязке закреплённого админа:
  refresh_commands(bot, config, user_id). Снятому админу личный список удаляется - остаётся общий.
Тексты команд - в app/texts.py (COMMANDS_*).
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from app import texts
from app.config import Config

log = logging.getLogger(__name__)

USER_COMMANDS = list(texts.COMMANDS_USER)
L1_COMMANDS = list(texts.COMMANDS_ADMIN_L1)
L2_COMMANDS = list(texts.COMMANDS_ADMIN_L2)
ADMIN_COMMAND_NAMES = tuple(c for c, _ in L1_COMMANDS + L2_COMMANDS)
L2_COMMAND_NAMES = tuple(c for c, _ in L2_COMMANDS)
ROLES = ("user", "l1", "l2", "owner")


def role_of(config: Config, user_id: int) -> str:
    if user_id in config.admin_ids:
        return "owner"
    level = config.admin_level(user_id)
    return "user" if level <= 0 else "l1" if level == 1 else "l2"


def menu_for(role: str) -> list[tuple[str, str]]:
    """Список (команда, описание) для меню Telegram этой роли."""
    pairs = list(USER_COMMANDS)
    if role in ("l1", "l2", "owner"):
        pairs += L1_COMMANDS
    if role in ("l2", "owner"):
        pairs += L2_COMMANDS
    if role == "owner":
        pairs = [(c, texts.COMMANDS_OWNER_MENU.get(c, d)) for c, d in pairs]
    return pairs


def commands_text(config: Config, user_id: int) -> str:
    """/commands: только доступные этому человеку команды, по разделам."""
    role = role_of(config, user_id)
    lines = [texts.COMMANDS_TITLE, "", texts.COMMANDS_FOR_ALL, *texts.COMMANDS_LINES_USER]
    if role in ("l1", "l2", "owner"):
        lines += ["", texts.COMMANDS_L1, *texts.COMMANDS_LINES_L1]
    if role in ("l2", "owner"):
        lines += ["", texts.COMMANDS_L2, *texts.COMMANDS_LINES_L2]
    if role == "owner":
        lines += ["", texts.COMMANDS_OWNER, *texts.COMMANDS_LINES_OWNER]
    if role in ("l2", "owner"):
        lines += ["", texts.COMMANDS_HINT]
    return "\n".join(lines)


def _commands(pairs: list[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in pairs]


async def remove_admin_commands(bot: Bot, chat_id: int) -> None:
    """Личный список удаляется - человек видит общий (как обычный пользователь)."""
    try:
        await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось убрать команды админа у %s: %s", chat_id, e)


async def refresh_commands(bot: Bot, config: Config, user_id: int) -> None:
    """Меню команд этого человека - по его текущей роли."""
    role = role_of(config, user_id)
    if role == "user":
        await remove_admin_commands(bot, user_id)
        return
    try:
        await bot.set_my_commands(_commands(menu_for(role)), scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as e:  # noqa: BLE001 - человек ещё не писал боту
        log.warning("команды для %s (%s) не установлены (%s): пусть он нажмёт /start", user_id, role, e)


async def set_commands(bot: Bot, config: Config) -> None:
    """При запуске: общий список и личные списки всех админов и владельцев."""
    try:
        await bot.set_my_commands(_commands(USER_COMMANDS), scope=BotCommandScopeDefault())
    except Exception:  # noqa: BLE001
        log.exception("не удалось установить список команд")
        return
    for admin_id in sorted(config.staff_ids):
        await refresh_commands(bot, config, admin_id)
