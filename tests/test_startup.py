"""Запуск как в проде: app.main.run поднимает БД, веб-сервер, планировщик и polling и аккуратно гасится."""
from __future__ import annotations

import asyncio
import contextlib

import aiohttp

from app import main
from tests.fakebot import make_bot


async def test_run_starts_web_and_polling_then_stops(config, monkeypatch, unused_tcp_port) -> None:
    bot, fake = make_bot()
    monkeypatch.setattr(main, "make_bot", lambda _config: bot)
    config.web_host, config.web_port = "127.0.0.1", unused_tcp_port
    task = asyncio.create_task(main.run(config))
    body = None
    async with aiohttp.ClientSession() as http:
        for _ in range(100):
            await asyncio.sleep(0.05)
            assert not task.done(), task.exception()
            with contextlib.suppress(aiohttp.ClientError):
                async with http.get(f"http://127.0.0.1:{unused_tcp_port}/health") as resp:
                    body = await resp.text()
                    break
    assert body == "ok"
    for _ in range(40):  # polling начался
        if any(type(m).__name__ == "GetUpdates" for m, _ in fake.log):
            break
        await asyncio.sleep(0.05)
    called = {type(m).__name__ for m, _ in fake.log}
    assert {"GetMe", "DeleteWebhook", "SetMyCommands", "GetUpdates", "GetChatMember"} <= called  # права в канале
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    async with aiohttp.ClientSession() as http:  # веб-сервер остановлен вместе с ботом
        try:
            async with http.get(f"http://127.0.0.1:{unused_tcp_port}/health") as resp:
                raise AssertionError(f"веб-сервер ещё отвечает: {resp.status}")
        except aiohttp.ClientConnectionError:
            pass
