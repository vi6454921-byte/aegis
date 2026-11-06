"""Точка входа: python -m app"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from app.config import get_config
from app.db.migrate import upgrade_head
from app.logging_setup import setup_logging


def main() -> None:
    config = get_config()
    config.ensure_dirs()
    setup_logging(config)
    log = logging.getLogger("app")
    if not config.bot_token.strip():
        log.error("BOT_TOKEN не задан. Скопируй .env.example в .env и впиши токен от @BotFather.")
        sys.exit(1)
    log.info("применяю миграции БД…")
    upgrade_head(config.database_url)  # до asyncio.run: alembic сам запускает свой event loop

    from app.main import run

    try:
        asyncio.run(run(config))
    except (KeyboardInterrupt, SystemExit):
        pass
    except TelegramUnauthorizedError:
        log.error("Telegram не принял BOT_TOKEN (Unauthorized). Проверь токен в .env: после /revoke в @BotFather "
                  "старый токен перестаёт работать: вставь новый.")
        sys.exit(1)
    except TelegramNetworkError as e:
        log.error("Нет связи с api.telegram.org (%s). Проверь интернет на сервере; в Docker бот перезапустится "
                  "сам.", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
