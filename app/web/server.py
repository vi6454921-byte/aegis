"""Mini App в том же процессе: решения /s/{token}/... (aiohttp). /g/{token} — старые кнопки подарков:
перенаправляем на текущую ссылку подарка (подарки теперь — ссылки в тексте сообщений)."""
from __future__ import annotations

import logging
import mimetypes
import re
from urllib.parse import quote

from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from app.db.models import Order
from app.services import gifts, solutions
from app.services.settings import load_settings

log = logging.getLogger(__name__)

CONFIG = web.AppKey("config", Config)
SESSIONMAKER = web.AppKey("sessionmaker", async_sessionmaker)
BOT_USERNAME = web.AppKey("bot_username", str)

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
SOLUTION_VISIBLE = ("in_work", "done", "accepted", "dispute")
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# С nosniff браузер не выполнит скрипт/стиль с неверным типом — задаём типы явно.
for _ext, _type in {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
                    ".svg": "image/svg+xml", ".json": "application/json", ".wasm": "application/wasm",
                    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp", ".ico": "image/x-icon", ".woff": "font/woff",
                    ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf", ".pdf": "application/pdf",
                    ".txt": "text/plain", ".csv": "text/csv", ".mp4": "video/mp4", ".mp3": "audio/mpeg"}.items():
    mimetypes.add_type(_type, _ext)

NOT_FOUND_HTML = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                  '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Не найдено</title>'
                  '</head><body style="font:16px system-ui,sans-serif;margin:32px;text-align:center">'
                  "<h2>Ссылка не найдена</h2><p>Открой нужный раздел заново через бота.</p></body></html>")


def _not_found() -> web.Response:
    return web.Response(status=404, text=NOT_FOUND_HTML, content_type="text/html", charset="utf-8",
                        headers=HEADERS)


def _html(data: bytes) -> web.Response:
    try:
        data.decode("utf-8")
        charset = "utf-8"
    except UnicodeDecodeError:
        charset = None  # пусть браузер возьмёт кодировку из <meta charset>
    return web.Response(body=data, content_type="text/html", charset=charset, headers=HEADERS)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def index(_: web.Request) -> web.Response:
    return web.Response(text="Аегис работает. Открывай материалы через бота.", headers=HEADERS)


async def gift_page(request: web.Request) -> web.StreamResponse:
    """Кнопки-Mini App подарков из сообщений до v2.1: редирект на текущую ссылку подарка из настроек."""
    token = request.match_info["token"]
    if not TOKEN_RE.fullmatch(token):
        return _not_found()
    sm: async_sessionmaker[AsyncSession] = request.app[SESSIONMAKER]
    async with sm() as session:
        link = await gifts.link_by_token(session, token)
        if link is None or link.gift not in gifts.GIFTS:
            return _not_found()
        url = gifts.gift_url(await load_settings(session), link.gift)
    if not url:
        return _not_found()
    raise web.HTTPFound(url, headers=HEADERS)


async def _solution_order(request: web.Request) -> Order | None:
    token = request.match_info["token"]
    if not TOKEN_RE.fullmatch(token):
        return None
    async with request.app[SESSIONMAKER]() as session:
        order = await session.scalar(select(Order).where(Order.solution_token == token))
    if order is None or not order.solution_entry or order.status not in SOLUTION_VISIBLE:
        return None
    return order


async def solution_root(request: web.Request) -> web.StreamResponse:
    order = await _solution_order(request)
    if order is None:
        return _not_found()
    # Редирект на входной файл: относительные ссылки внутри решения (картинки, css) начинают работать.
    raise web.HTTPFound(f"/s/{request.match_info['token']}/{quote(order.solution_entry)}", headers=HEADERS)


async def solution_file(request: web.Request) -> web.StreamResponse:
    order = await _solution_order(request)
    if order is None:
        return _not_found()
    rel = request.match_info["path"] or order.solution_entry
    path = solutions.resolve(request.app[CONFIG].solutions_dir, order.id, rel)
    if path is None:
        return _not_found()
    if path.suffix.lower() in solutions.HTML_EXT:
        return _html(path.read_bytes())
    return web.FileResponse(path, headers=HEADERS)


def create_app(config: Config, sessionmaker: async_sessionmaker[AsyncSession], bot_username: str) -> web.Application:
    app = web.Application()
    app[CONFIG] = config
    app[SESSIONMAKER] = sessionmaker
    app[BOT_USERNAME] = bot_username
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/g/{token}", gift_page)
    app.router.add_get("/s/{token}", solution_root)
    app.router.add_get("/s/{token}/{path:.*}", solution_file)
    return app


async def start_web(config: Config, sessionmaker: async_sessionmaker[AsyncSession], bot_username: str) -> web.AppRunner:
    runner = web.AppRunner(create_app(config, sessionmaker, bot_username), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, config.web_host, config.web_port).start()
    log.info("веб-сервер Mini App слушает %s:%s", config.web_host, config.web_port)
    return runner
