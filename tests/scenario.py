"""Сценарий проверки на двух аккаунтах из ТЗ (шаги 1–8) — общий для тестов.

Аккаунт 1 — админ (ADMIN_IDS) и пригласивший, аккаунт 2 — друг-клиент.
"""
from __future__ import annotations

import csv
import html
import itertools
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from aiohttp.test_utils import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import texts
from app.bot.callbacks import AdminCb
from app.config import Config
from app.db.models import Order, OrderFile, Payment, Referral, User
from app.services.maintenance import integrity_issues
from app.services.settings import set_setting
from app.utils.money import fmt_rub
from app.utils.timeutil import WEEKDAYS, local_today, to_local
from tests.conftest import ADMIN_ID
from tests.fakebot import Harness

A1, A2 = ADMIN_ID, 2002
GIFT_URLS = ("https://fgdg346346hgjghj.github.io/Aegis-14/#z1a",  # расчётка
             "https://fgdg346346hgjghj.github.io/Aegis-/#start",  # начерталка
             "https://fgdg346346hgjghj.github.io/matric/")  # матрицы
SOLUTION = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Решение #1</title></head>"
            "<body><h1>Задача 1</h1><p>Эпюра Монжа и ответ.</p></body></html>").encode()


_n = itertools.count(1)


def path(url: str) -> str:
    return urlparse(url).path


def gift_hrefs(text: str) -> list[str]:
    """Адреса ссылок <a href> в тексте сообщения (по порядку)."""
    return [html.unescape(u) for u in re.findall(r'<a href="([^"]+)">', text or "")]


async def get(sessionmaker: async_sessionmaker[AsyncSession], model, key):  # noqa: ANN001, ANN201
    async with sessionmaker() as db:
        return await db.get(model, key)


async def set_settings(sessionmaker: async_sessionmaker[AsyncSession], **values) -> None:  # noqa: ANN003
    async with sessionmaker() as db:
        for key, value in values.items():
            await set_setting(db, key, value)
        await db.commit()


async def run_scenario(h: Harness, web: TestClient, config: Config,
                       sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Проходит шаги 1–8 и проверяет БД, сообщения, веб и CSV. Возвращает id заказа."""
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await set_settings(sessionmaker, referral_bonus=5000)  # по умолчанию 0: основной бонус — за подписку на канал

    # ---- 0. Аккаунт 1 запускает бота и берёт свою реф-ссылку
    await h.send(A1, "/start")
    assert texts.ADMIN_HINT in h.last_text(A1)
    await h.send(A1, texts.MAIN_MENU["invite"])
    ref = re.search(r"start=(ref_[\w-]+)", h.last_text(A1)).group(1)

    # ---- 1. Аккаунт 2 заходит по реф-ссылке → в приветствии подарки: три ссылки прямо в тексте, без кнопок
    await h.send(A2, f"/start {ref}")
    assert texts.REF_NEW_FRIEND in h.texts(A1)
    welcome, welcome_kb = next((m, kb) for m, kb in h.screen(A2) if (m.text or "").startswith("Привет!"))
    assert gift_hrefs(welcome.text) == list(GIFT_URLS)
    assert welcome_kb is None  # ни кнопок, ни Mini App, ни файлов
    assert not h.sent_documents(A2)
    friend = await get(sessionmaker, User, A2)
    assert friend.referrer_id == A1 and friend.source == "ref"

    # ---- 2. Заказ по начерталке: 1 задача, дедлайн через 4 дня, фото лекции и практики
    await h.send(A2, texts.MAIN_MENU["order"])
    await h.click(A2, texts.SUBJECTS["nach"])
    await h.send(A2, photo="cond-1")
    assert "1" in h.last_text(A2)  # «получил фото: 1»
    await h.click(A2, "✅ Готово")
    await h.click(A2, "1", exact=True)
    await h.click(A2, texts.BTN_NO_VARIANT)
    await h.click(A2, texts.DEADLINE_BTNS["d3"])
    day = local_today() + timedelta(days=4)
    await h.click(A2, f"{WEEKDAYS[day.weekday()]} {day:%d.%m}")
    await h.send(A2, photo="lecture-1")
    await h.click(A2, "✅ Готово")
    await h.send(A2, photo="practice-1")
    await h.click(A2, "✅ Готово")
    await h.click(A2, "⏭ Без комментария")
    summary = h.last_text(A2)
    assert "Конспекты: лекция — 1 фото, практика — 1 фото" in summary
    assert f"<b>{fmt_rub(20000)}</b>" in summary and f"<b>{fmt_rub(10000)}</b>" in summary
    await h.click(A2, texts.SUBMIT_BTN)
    await h.click(A2, texts.TERMS_ACCEPT_BTN)  # первый заказ — сначала принять условия
    async with sessionmaker() as db:
        order = await db.scalar(select(Order).where(Order.user_id == A2))
        kinds = dict((await db.execute(select(OrderFile.kind, func.count(OrderFile.id))
                                       .where(OrderFile.order_id == order.id).group_by(OrderFile.kind))).all())
        local_files = (await db.execute(select(OrderFile.local_path).where(OrderFile.order_id == order.id))).scalars()
        assert all(p and Path(p).is_file() for p in local_files)
    oid = order.id
    assert texts.SUBMITTED.format(id=oid, notes=" и конспекты") in h.texts(A2)
    assert order.status == "review" and order.task_count == 1 and order.subject == "nach"
    assert to_local(order.deadline_at).date() == day
    assert (order.lecture_status, order.practice_status) == ("pending", "pending")
    assert kinds == {"condition": 1, "lecture": 1, "practice": 1}
    assert (await get(sessionmaker, User, A2)).terms_accepted_at is not None

    # ---- 3. Аккаунт 1 видит заказ, одобряет оба конспекта → 100 ₽ → выставляет счёт
    assert any(t.startswith(f"📥 Новый заказ #{oid}") for t in h.texts(A1))
    await h.click(A1, "📦 Открыть заказ")
    h.find_button(A1, f"💳 Выставить счёт · {fmt_rub(20000)}")
    await h.click(A1, "✅ Лекция")
    await h.click(A1, "✅ Практика")
    await h.click(A1, f"💳 Выставить счёт · {fmt_rub(10000)}")
    order = await get(sessionmaker, Order, oid)
    assert order.status == "awaiting_payment" and order.price_final == 10000
    assert (order.lecture_status, order.practice_status) == ("approved", "approved")
    assert any(t.startswith(f"💳 Заказ #{oid} проверен. К оплате: <b>{fmt_rub(10000)}</b>") for t in h.texts(A2))

    # ---- 4. Аккаунт 2 оплачивает тестовыми деньгами
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(10000)}")
    assert "🧪 Тестовый режим" in h.last_text(A2)
    await h.click(A2, texts.PAY_TEST_BTN)
    assert texts.PAY_DONE_ORDER.format(id=oid) in h.texts(A2)
    order = await get(sessionmaker, Order, oid)
    assert order.status == "paid" and order.is_test and order.paid_invoice == 10000
    async with sessionmaker() as db:
        pay = await db.scalar(select(Payment).where(Payment.order_id == oid))
    assert (pay.provider, pay.status, pay.amount_rub, pay.is_test) == ("test", "paid", 10000, True)

    # ---- 5. Аккаунт 1: в работу → HTML-решение → «Готово» → ему +50 бонусов за друга
    await h.click(A1, "📦 Открыть заказ")
    await h.click(A1, "🛠 Взять в работу")
    assert texts.ORDER_TAKEN.format(id=oid) in h.texts(A2)
    await h.click(A1, "📤 Загрузить решение")
    await h.send(A1, document=("sol-1", "solution.html", SOLUTION))
    assert h.last_text(A1).startswith("Получено файлов: 1 — solution.html")
    await h.click(A1, "💾 Сохранить (1)")
    await h.click(A1, "📦 Готово — отправить клиенту")
    assert texts.ORDER_DONE.format(id=oid) in h.texts(A2)
    assert any(t.startswith(f"🎉 Твой друг получил первый оплаченный заказ — тебе +{fmt_rub(5000)}")
               for t in h.texts(A1))
    assert f"начислено +{fmt_rub(5000)} бонусами" in h.last_text(A1)
    order = await get(sessionmaker, Order, oid)
    referrer = await get(sessionmaker, User, A1)
    assert order.status == "done" and order.solution_entry == "solution.html"
    assert referrer.balance_bonus == 5000
    async with sessionmaker() as db:
        r = await db.scalar(select(Referral).where(Referral.referred_id == A2))
    assert r.rewarded and r.reward_amount == 5000 and r.reward_order_id == oid

    # ---- 6. Аккаунт 2 открывает решение, жмёт «Не зачли» и присылает скрин
    _, sol_btn = h.find_button(A2, texts.OPEN_SOLUTION)
    assert sol_btn.web_app is not None
    resp = await web.get(path(sol_btn.web_app.url))  # /s/{token} → редирект на /s/{token}/solution.html
    assert resp.status == 200 and resp.url.path.endswith("/solution.html")
    assert "Эпюра Монжа" in await resp.text()
    await h.click(A2, texts.DISPUTE_BTN)
    assert h.last_text(A2) == texts.DISPUTE_PROMPT
    await h.send(A2, photo="teacher-remark")
    assert h.last_text(A2) == texts.DISPUTE_SENT
    assert any(t.startswith(f"❌ «Не зачли» по заказу #{oid}") for t in h.texts(A1))
    order = await get(sessionmaker, Order, oid)
    assert order.status == "dispute"

    # ---- 7. Аккаунт 1: «Вернуть деньги» → у аккаунта 2 баланс, у аккаунта 1 отозваны 50 бонусов
    await h.click(A1, "↩️ Вернуть деньги")  # кнопка под уведомлением о споре (у админа 2 уровня)
    assert f"Бонус пригласившему ({fmt_rub(5000)}) будет отозван" in h.last_text(A1)
    await h.click(A1, "↩️ На баланс")
    assert texts.REFUNDED.format(id=oid, details=f"{fmt_rub(10000)} — на рублёвый баланс") in h.texts(A2)
    assert texts.REF_REVOKED.format(amount=fmt_rub(5000)) in h.texts(A1)
    order = await get(sessionmaker, Order, oid)
    friend = await get(sessionmaker, User, A2)
    referrer = await get(sessionmaker, User, A1)
    assert order.status == "refunded"
    assert (friend.balance_rub, friend.balance_bonus) == (10000, 0)
    assert (referrer.balance_rub, referrer.balance_bonus) == (0, 0)
    async with sessionmaker() as db:
        pay = await db.scalar(select(Payment).where(Payment.order_id == oid))
        r = await db.scalar(select(Referral).where(Referral.referred_id == A2))
    assert pay.status == "refunded" and not r.rewarded and r.revoked_count == 1
    assert (await web.get(path(sol_btn.web_app.url))).status == 404  # после возврата решение закрыто

    # ---- 8. Статистика, карточки пользователей и CSV сходятся с тем, что произошло
    await h.send(A1, "/admin")
    await h.click(A1, "📊 Статистика")
    report = h.last_text(A1)
    assert "всего <b>2</b>" in report and "рефералы 1" in report and "прямые 1" in report
    assert f"{texts.STATUS['refunded']} 1" in report
    assert f"Поступило по счетам: {fmt_rub(0)} | {fmt_rub(10000)}" in report
    assert f"Возвраты на баланс: {fmt_rub(0)} | {fmt_rub(10000)}" in report
    assert f"рублями {fmt_rub(10000)}, бонусами {fmt_rub(0)}" in report

    await h.click(A1, "🛡 В админку")
    await h.click(A1, "📥 Заказы")
    await h.click(A1, "• Очередь")  # фильтр уже выбран — перерисовка без изменений
    await h.click(A1, "Закрытые")
    await h.click(A1, f"#{oid} ·")
    await h.click(A1, "👤 Клиент")
    card = h.last_text(A1)
    assert f"💰 Рубли: <b>{fmt_rub(10000)}</b> · 🎁 Бонусы: <b>{fmt_rub(0)}</b>" in card
    assert "пригласил" in card and "Условия: приняты" in card
    assert "📦 Заказов: 1, оплачено: 1" in card

    await h.click(A1, "⬅️ К пользователям")
    await h.click(A1, "Аня")
    card = h.last_text(A1)
    assert f"💰 Рубли: <b>{fmt_rub(0)}</b> · 🎁 Бонусы: <b>{fmt_rub(0)}</b>" in card
    assert "👥 Друзей: 1, оплатили: 0" in card  # бонус за друга отозван вместе с возвратом

    await h.click(A1, "🛡 В админку")
    await h.click(A1, "💾 Данные")
    await h.click(A1, "📤 Экспорт CSV")
    exports = {p.name.split("-")[0]: p for p in config.exports_dir.glob("*.csv")}
    assert set(exports) == {"users", "orders", "transactions", "payments"}

    def rows(name: str) -> list[dict[str, str]]:
        with open(exports[name], encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh, delimiter=";"))

    users = {int(u["id"]): u for u in rows("users")}
    assert set(users) == {A1, A2}
    assert (users[A2]["balance_rub"], users[A2]["balance_bonus"], users[A2]["referrer_id"]) == ("10000", "0", str(A1))
    assert (users[A1]["balance_rub"], users[A1]["balance_bonus"]) == ("0", "0")
    [o] = rows("orders")
    assert (o["id"], o["status"], o["price_final"], o["paid_invoice"], o["is_test"]) == (
        str(oid), "refunded", "10000", "10000", "1")
    [p] = rows("payments")
    assert (p["provider"], p["status"], p["amount_rub"], p["is_test"]) == ("test", "refunded", "10000", "1")
    tx = [(t["user_id"], t["account"], t["kind"], int(t["amount"])) for t in rows("transactions")]
    assert sorted(tx) == sorted([
        (str(A2), "rub", "payment_in", 10000),  # оплата счёта заходит на баланс…
        (str(A2), "rub", "order_pay", -10000),  # …и сразу списывается за заказ
        (str(A1), "bonus", "referral", 5000),
        (str(A2), "rub", "refund", 10000),
        (str(A1), "bonus", "referral_revoke", -5000),
    ]), tx
    sent_csv = [m for m, _ in h.fake.log if type(m).__name__ == "SendMediaGroup" and m.chat_id == A1]
    assert len(sent_csv) == 1 and len(sent_csv[0].media) == 4

    async with sessionmaker() as db:
        assert await integrity_issues(db) == []
    return oid


# ---------------------------------------------------------------- короткие шаги для других сценариев
async def start_with_referral(h: Harness, referrer: int, friend: int) -> None:
    await h.send(referrer, "/start")
    await h.send(referrer, texts.MAIN_MENU["invite"])
    ref = re.search(r"start=(ref_[\w-]+)", h.last_text(referrer)).group(1)
    await h.send(friend, f"/start {ref}")


async def wizard_order(h: Harness, uid: int, *, notes: bool, days: int = 4) -> int:
    """Оформляет заказ по начерталке на 1 задачу через мастер. Возвращает id заказа."""
    await h.send(uid, texts.MAIN_MENU["order"])
    await h.click(uid, texts.SUBJECTS["nach"])
    await h.send(uid, photo=f"cond-{uid}-{next(_n)}")
    await h.click(uid, "✅ Готово")
    await h.click(uid, "1", exact=True)
    await h.click(uid, texts.BTN_NO_VARIANT)
    await h.click(uid, texts.DEADLINE_BTNS["d3"])
    day = local_today() + timedelta(days=days)
    await h.click(uid, f"{WEEKDAYS[day.weekday()]} {day:%d.%m}")
    for kind in ("lecture", "practice"):
        if notes:
            await h.send(uid, photo=f"{kind}-{uid}-{next(_n)}")
            await h.click(uid, "✅ Готово")
        else:
            await h.click(uid, texts.BTN_SKIP)
    await h.click(uid, "⏭ Без комментария")
    await h.click(uid, texts.SUBMIT_BTN)
    _, markup = h.screen(uid)[-1]
    if markup and any(b.text == texts.TERMS_ACCEPT_BTN for row in markup.inline_keyboard for b in row):
        await h.click(uid, texts.TERMS_ACCEPT_BTN)
    return int(re.search(r"Заказ #(\d+) отправлен", h.last_text(uid)).group(1))


async def admin_invoice(h: Harness, oid: int, price: int, *, approve_notes: bool = False) -> None:
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    if approve_notes:
        await h.click(A1, "✅ Лекция")
        await h.click(A1, "✅ Практика")
    await h.click(A1, f"💳 Выставить счёт · {fmt_rub(price)}")


async def user_pays(h: Harness, uid: int, price: int) -> None:
    await h.click(uid, f"{texts.PAY_BTN} · {fmt_rub(price)}")
    await h.click(uid, texts.PAY_TEST_BTN)


async def upload_solution(h: Harness, content: bytes, name: str = "solution.html") -> None:
    await h.send(A1, document=(f"sol-{next(_n)}", name, content))
    await h.click(A1, "💾 Сохранить (1)")


async def admin_deliver(h: Harness, oid: int, content: bytes = SOLUTION) -> None:
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    await h.click(A1, "🛠 Взять в работу")
    await h.click(A1, "📤 Загрузить решение")
    await upload_solution(h, content)
    await h.click(A1, "📦 Готово — отправить клиенту")
