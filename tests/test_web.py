"""Веб-часть Mini App: решения как мини-сайт, защита от обхода путей, старые ссылки подарков → редирект."""
from __future__ import annotations

import io
import secrets
import zipfile

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from app.db.models import GiftLink, GiftOpen
from app.services import solutions
from app.services.settings import set_setting
from app.web.server import create_app
from tests.helpers import make_order, make_user

BOT = "aegis_test_bot"


@pytest.fixture
async def web(config, sessionmaker):
    client = TestClient(TestServer(create_app(config, sessionmaker, BOT)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def order_with_solution(session, s, config, files: dict[str, bytes], status: str = "done"):  # noqa: ANN001, ANN201
    user = await make_user(session, 3003)
    order = await make_order(session, s, user.id)
    staging = solutions.reset_staging(config.solutions_dir, order.id)
    for name, data in files.items():
        (staging / name).write_bytes(data)
    order.solution_entry = solutions.finalize(config.solutions_dir, order.id)
    order.solution_token = secrets.token_urlsafe(24)
    order.status = status
    await session.commit()
    return order


async def test_health_and_index(web) -> None:
    assert await (await web.get("/health")).text() == "ok"
    resp = await web.get("/")
    assert resp.status == 200 and resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("url", ["/g/short", "/g/" + "x" * 30, "/s/" + "y" * 30, "/s/" + "y" * 30 + "/index.html",
                                 "/g/..%2F..%2Fetc%2Fpasswd", "/nope"])
async def test_unknown_tokens_are_404(web, url: str) -> None:
    assert (await web.get(url, allow_redirects=False)).status == 404


async def test_old_gift_buttons_redirect_to_current_links(web, session) -> None:
    """Кнопки-Mini App подарков из сообщений до v2.1 (/g/{токен}) ведут на текущую ссылку подарка."""
    user = await make_user(session, 3003)
    token = secrets.token_urlsafe(24)
    session.add(GiftLink(user_id=user.id, gift="matrices", token=token))
    await session.commit()
    resp = await web.get(f"/g/{token}", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "https://fgdg346346hgjghj.github.io/matric/"
    await set_setting(session, "gift_url_matrices", "https://example.github.io/matrix/")
    await session.commit()
    resp = await web.get(f"/g/{token}", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "https://example.github.io/matrix/"
    await set_setting(session, "gift_url_matrices", "")  # подарок скрыт
    await session.commit()
    assert (await web.get(f"/g/{token}", allow_redirects=False)).status == 404
    assert await session.scalar(select(func.count(GiftOpen.id))) == 0  # открытия по ссылкам больше не считаем


async def test_solution_zip_is_served_as_site(web, session, s, config) -> None:
    site = zip_bytes({"site/index.html": b"<html><img src='img/a.png'><link href='style.css'></html>",
                      "site/img/a.png": b"\x89PNG\r\n\x1a\nfake", "site/style.css": b"body{color:red}",
                      "__MACOSX/site/._index.html": b"junk"})
    order = await order_with_solution(session, s, config, {"solution.zip": site})
    assert order.solution_entry == "site/index.html"
    root = await web.get(f"/s/{order.solution_token}", allow_redirects=False)
    assert root.status == 302 and root.headers["Location"] == f"/s/{order.solution_token}/site/index.html"
    page = await web.get(f"/s/{order.solution_token}")
    assert page.status == 200 and "img/a.png" in await page.text()
    assert page.headers["Cache-Control"] == "no-store"
    img = await web.get(f"/s/{order.solution_token}/site/img/a.png")
    assert img.status == 200 and img.content_type == "image/png"
    css = await web.get(f"/s/{order.solution_token}/site/style.css")
    assert css.status == 200 and css.content_type == "text/css"
    assert not (config.solutions_dir / str(order.id) / "__MACOSX").exists()


@pytest.mark.parametrize("evil", ["../../aegis.db", "..%2F..%2Faegis.db", "%2e%2e/%2e%2e/aegis.db", "/etc/passwd",
                                  "site/../../../aegis.db"])
async def test_solution_path_traversal_blocked(web, session, s, config, evil: str) -> None:
    order = await order_with_solution(session, s, config, {"index.html": b"<html>ok</html>"})
    resp = await web.get(f"/s/{order.solution_token}/{evil}", allow_redirects=False)
    assert resp.status == 404


async def test_solution_hidden_after_refund(web, session, s, config) -> None:
    order = await order_with_solution(session, s, config, {"index.html": b"<html>ok</html>"})
    assert (await web.get(f"/s/{order.solution_token}/index.html")).status == 200
    order.status = "refunded"
    await session.commit()
    assert (await web.get(f"/s/{order.solution_token}/index.html")).status == 404


async def test_several_html_files_get_generated_index(web, session, s, config) -> None:
    order = await order_with_solution(session, s, config, {"task1.html": b"<p>1</p>", "task2.html": b"<p>2</p>"})
    assert order.solution_entry == solutions.INDEX_NAME
    page = await (await web.get(f"/s/{order.solution_token}")).text()
    assert 'href="task1.html"' in page and 'href="task2.html"' in page


@pytest.mark.parametrize("bad, message", [
    ({"a.zip": zip_bytes({"../evil.html": b"x"})}, "Недопустимый путь"),
    ({"a.zip": zip_bytes({"/abs.html": b"x"})}, "Недопустимый путь"),
    ({"a.zip": zip_bytes({"readme.txt": b"x"})}, "нет HTML"),
    ({"a.zip": b"not a zip"}, "повреждён"),
])
async def test_bad_solution_archives_rejected(session, s, config, bad: dict[str, bytes], message: str) -> None:
    user = await make_user(session, 3003)
    order = await make_order(session, s, user.id)
    staging = solutions.reset_staging(config.solutions_dir, order.id)
    for name, data in bad.items():
        (staging / name).write_bytes(data)
    with pytest.raises(solutions.SolutionError, match=message):
        solutions.finalize(config.solutions_dir, order.id)
    assert not (config.solutions_dir / str(order.id)).exists()
    assert not (config.solutions_dir.parent / "evil.html").exists()
    count = await session.scalar(select(func.count(GiftOpen.id)))
    assert count == 0


def test_safe_name() -> None:
    assert solutions.safe_name("../../etc/passwd") == "passwd"
    assert solutions.safe_name("C:\\Users\\x\\Решение 1.html") == "Решение 1.html"
    assert solutions.safe_name("..") == "file.html"
