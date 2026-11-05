"""Сценарий проверки на двух аккаунтах из ТЗ — целиком, через настоящий диспетчер, БД и веб-сервер.

Telegram подменён (tests/fakebot.py): бот думает, что общается с API, а тест «нажимает» кнопки и шлёт файлы.
Сами шаги — в tests/scenario.py.
"""
from __future__ import annotations

from tests.scenario import run_scenario


async def test_two_accounts_scenario(e2e, config, sessionmaker) -> None:
    h, web = e2e
    await run_scenario(h, web, config, sessionmaker)
