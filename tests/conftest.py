"""Общие фикстуры: каждая проверка получает свою копию чистой БД (миграции Alembic применены один раз)."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.bot import files
from app.bot.middlewares import AntiFloodMiddleware
from app.config import Config
from app.db.engine import make_engine, make_sessionmaker
from app.db.migrate import upgrade_head
from app.services import settings as settings_service
from app.services.settings import load_settings
from app.web.server import create_app

ADMIN_ID = 1001
BOT_USERNAME = "aegis_test_bot"


@pytest.fixture(autouse=True)
def _isolated_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты никогда не трогают настоящий .env проекта (туда пишется ADMIN_IDS при «/admin КОД»)."""
    from app.services import channel, owner

    monkeypatch.setattr(owner, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(channel, "health", channel.Health())  # «бот не админ канала» не протекает между тестами


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("template") / "aegis.db"
    errors: list[BaseException] = []

    def migrate() -> None:  # alembic сам крутит asyncio.run — запускаем вне чужого event loop
        try:
            upgrade_head(f"sqlite+aiosqlite:///{db}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=migrate)
    t.start()
    t.join()
    if errors:
        raise errors[0]
    return db


@pytest.fixture
def config(tmp_path: Path, template_db: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "aegis.db"
    shutil.copyfile(template_db, db)
    cfg = Config(_env_file=None, BOT_TOKEN="123456:TEST-TOKEN", ADMIN_IDS=str(ADMIN_ID), BOT_USERNAME=BOT_USERNAME,
                 DATABASE_URL=f"sqlite+aiosqlite:///{db}", DATA_DIR=str(data),
                 WEBAPP_BASE_URL="https://aegis.example.com", PAYMENTS_MODE="test")
    cfg.ensure_dirs()
    settings_service.configure(cfg)
    return cfg


@pytest.fixture
async def sessionmaker(config: Config):
    engine = make_engine(config.database_url)
    try:
        yield make_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(sessionmaker):
    async with sessionmaker() as s:
        yield s


@pytest.fixture
async def s(session):
    return await load_settings(session)


@pytest.fixture
async def e2e(config, sessionmaker, monkeypatch):
    """Настоящий диспетчер со всеми роутерами + фейковый Telegram + веб-сервер Mini App."""
    from app.main import build_dispatcher
    from tests.fakebot import Harness, make_bot

    monkeypatch.setattr(files, "ACK_DELAY", 0.01)
    bot, fake = make_bot()
    dp = build_dispatcher(config, sessionmaker, BOT_USERNAME)
    for mw in dp.update.outer_middleware:
        if isinstance(mw, AntiFloodMiddleware):
            mw.burst = 10**6  # тест жмёт кнопки быстрее человека
    h = Harness(dp, bot, fake)
    web = TestClient(TestServer(create_app(config, sessionmaker, BOT_USERNAME)))
    await web.start_server()
    try:
        yield h, web
    finally:
        h.close()
        await web.close()
        await bot.session.close()
