"""Первый админ без правки .env: бот печатает код в консоль, владелец шлёт «/admin КОД»."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from app import main, texts
from app.services import owner
from tests.fakebot import make_bot


def admin_scopes(h_or_calls) -> set[int]:  # noqa: ANN001
    calls = h_or_calls.calls("SetMyCommands") if hasattr(h_or_calls, "calls") else h_or_calls
    return {m.scope.chat_id for m in calls if getattr(m.scope, "chat_id", None) is not None}


async def test_owner_becomes_admin_with_console_code(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    env = owner.ENV_FILE
    env.write_text("BOT_TOKEN=123:abc\n# Твой Telegram id\nADMIN_IDS=\nBOT_USERNAME=aegis_test_bot\n", encoding="utf-8")
    config.admin_ids_raw = ""
    claim = h.dp["owner_claim"]
    code = claim.start()
    h.user(501, "Владелец")
    h.user(502, "Чужой")

    await h.send(502, "/admin")
    assert h.last_text(502) == texts.OWNER_HINT
    await h.send(502, "/admin " + ("111111" if code != "111111" else "222222"))
    assert h.last_text(502) == texts.OWNER_HINT and not config.admin_ids

    await h.send(501, f"/admin {code}")
    assert h.last_text(501) == texts.OWNER_GRANTED
    assert config.admin_ids == {501} and not claim.active
    assert 501 in admin_scopes(h)  # у владельца в меню команд появилась /admin
    text = env.read_text(encoding="utf-8")
    assert "\nADMIN_IDS=501\n" in text and "BOT_TOKEN=123:abc" in text and "# Твой Telegram id" in text
    async with sessionmaker() as session:
        assert await owner.load_saved(session) == {501}

    await h.send(501, "/admin")
    assert "Админка" in h.last_text(501)
    await h.send(502, f"/admin {code}")  # код одноразовый
    assert h.last_text(502) == texts.ADMIN_ONLY and config.admin_ids == {501}


async def test_no_claim_when_admin_configured(e2e, config) -> None:
    h, _ = e2e
    h.user(503, "Гость")
    await h.send(503, "/admin 123456")
    assert h.last_text(503) == texts.ADMIN_ONLY and not h.dp["owner_claim"].active


async def test_code_rotates_after_wrong_attempts(e2e, config, caplog) -> None:
    h, _ = e2e
    config.admin_ids_raw = ""
    claim = h.dp["owner_claim"]
    first = claim.start()
    wrong = "100000" if first != "100000" else "100001"
    h.user(504, "Перебор")
    for _ in range(owner.MAX_FAILURES):
        await h.send(504, f"/admin {wrong}")
    assert claim.active and claim.code != first
    assert any(f"/admin {claim.code}" in r.getMessage() for r in caplog.records)  # новый код — в консоли
    await h.send(504, f"/admin {first}")
    assert h.last_text(504) == texts.OWNER_HINT and not config.admin_ids


def test_save_to_env_appends_when_line_missing(tmp_path) -> None:
    env = tmp_path / ".env"
    assert not owner.save_to_env({7}, env)  # файла нет (Docker) — не создаём
    env.write_text("BOT_TOKEN=1:x", encoding="utf-8")
    assert owner.save_to_env({9, 7}, env)
    assert env.read_text(encoding="utf-8") == "BOT_TOKEN=1:x\nADMIN_IDS=7,9\n"


async def _run_until_polling(config, monkeypatch, port):  # noqa: ANN001, ANN202
    bot, fake = make_bot()
    monkeypatch.setattr(main, "make_bot", lambda _config: bot)
    config.web_host, config.web_port = "127.0.0.1", port
    task = asyncio.create_task(main.run(config))
    for _ in range(200):
        await asyncio.sleep(0.02)
        assert not task.done(), task.exception()
        if any(type(m).__name__ == "GetUpdates" for m, _ in fake.log):
            break
    else:
        raise AssertionError("polling не начался")
    return task, fake


async def _stop(task) -> None:  # noqa: ANN001
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_prints_code_when_no_admins(config, monkeypatch, unused_tcp_port, caplog) -> None:
    caplog.set_level(logging.INFO)
    config.admin_ids_raw = ""
    task, _ = await _run_until_polling(config, monkeypatch, unused_tcp_port)
    try:
        banners = [r.getMessage() for r in caplog.records if "Администратор ещё не назначен" in r.getMessage()]
        assert len(banners) == 1 and re.search(r"/admin \d{6}\n", banners[0])
        assert "t.me/aegis_test_bot" in banners[0]
    finally:
        await _stop(task)


async def test_run_restores_owner_saved_in_db(config, sessionmaker, monkeypatch, unused_tcp_port, caplog) -> None:
    config.admin_ids_raw = ""
    async with sessionmaker() as session:
        await owner.grant(session, config, 777)  # владелец назначен кодом в прошлый запуск
    config.admin_ids_raw = ""  # новый запуск, ADMIN_IDS в .env по-прежнему пуст
    caplog.set_level(logging.INFO)
    task, fake = await _run_until_polling(config, monkeypatch, unused_tcp_port)
    try:
        assert config.admin_ids == {777}
        assert 777 in admin_scopes([m for m, _ in fake.log if type(m).__name__ == "SetMyCommands"])
        assert not any("Администратор ещё не назначен" in r.getMessage() for r in caplog.records)
    finally:
        await _stop(task)


async def test_env_admin_ids_win_over_saved(config, sessionmaker) -> None:
    async with sessionmaker() as session:
        await owner.grant(session, config, 888)
        config.admin_ids_raw = "1001"
        assert not await owner.restore_saved(session, config)
    assert config.admin_ids == {1001}
