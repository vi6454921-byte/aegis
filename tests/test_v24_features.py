"""Новое в 2.4: гонка при закреплении админов, журнал действий, «📎 Любая задача» (оценка цены админом),
тексты без длинных тире, цены без наценок, кнопка «💸 Вывести» в балансе."""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from sqlalchemy import func, select

from app import actionlog, texts
from app.bot import guide
from app.db.engine import make_engine, make_sessionmaker
from app.db.migrate import alembic_config, upgrade_head
from app.db.models import Admin, Order, OrderFile, PinnedAdmin, Referral, User
from app.logging_setup import ACTIONS, NetworkNoiseFilter, RedactingFormatter
from app.services import ledger, pinned
from app.services import orders as svc
from app.services.admin_log import log_admin
from app.services.settings import load_settings
from app.services.users import register_user
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc
from tests.helpers import make_order, make_user, tg
from tests.scenario import A1, A2, admin_deliver, get, set_settings, user_pays

M = texts.MAIN_MENU
APP = Path(__file__).resolve().parent.parent / "app"
SENIOR = "dimaslavikov1"  # есть в SENIOR_ADMIN_USERNAMES по умолчанию
LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| (@\w+|без username) \(id \d+\) \| \S")


def kb_rows(h, uid: int) -> list[list[str]]:  # noqa: ANN001
    _, markup = h.screen(uid)[-1]
    return [[b.text for b in row] for row in (markup.inline_keyboard if markup else [])]


def actions(caplog) -> list[str]:  # noqa: ANN001
    return [r.getMessage() for r in caplog.records if r.name == ACTIONS]


# ================================================================ 1. гонка при закреплении админов
async def test_five_parallel_starts_from_senior_admin_create_one_row(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    uid = 7007
    h.user(uid, "Дима", username=SENIOR)
    await asyncio.gather(*(h.send(uid, "/start") for _ in range(5)))
    h.assert_no_errors()
    assert texts.ERROR_USER not in h.texts(uid)
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(User.id)).where(User.id == uid)) == 1
        assert await db.scalar(select(func.count(PinnedAdmin.id)).where(PinnedAdmin.username == SENIOR)) == 1
        rows = (await db.execute(select(Admin).where(Admin.user_id == uid))).scalars().all()
    assert [(a.level, a.pinned) for a in rows] == [(2, True)]
    assert config.admin_level(uid) == 2
    assert texts.PINNED_WELCOME in h.texts(uid)


async def test_pinning_is_idempotent_without_the_user_lock(config, sessionmaker) -> None:
    """Даже без замка в middleware (другой процесс, повторный апдейт) одновременные вызовы не падают."""
    async with sessionmaker() as db:
        await make_user(db, 7008)
    user = SimpleNamespace(id=7008, username=SENIOR, first_name="Дима", full_name="Дима")
    sessions = [sessionmaker() for _ in range(5)]
    try:
        results = await asyncio.gather(*(pinned.on_user(s, config, user, now_utc()) for s in sessions))
    finally:
        for s in sessions:
            await s.close()
    assert [r.kind for r in results if r is not None] == ["bound"]
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(PinnedAdmin.id))) == 1
        assert [(a.user_id, a.level) for a in (await db.execute(select(Admin))).scalars()] == [(7008, 2)]
        assert await pinned.on_user(db, config, user, now_utc()) is None  # повторный вызов ничего не меняет
        assert await db.scalar(select(func.count(Admin.user_id))) == 1


async def test_parallel_first_registration_with_referral(sessionmaker) -> None:
    async with sessionmaker() as db:
        friend = await make_user(db, 2001)
        code = friend.ref_code
    sessions = [sessionmaker() for _ in range(5)]
    try:
        regs = await asyncio.gather(*(register_user(s, tg(7009, "Новый"), f"ref_{code}", now_utc())
                                      for s in sessions))
    finally:
        for s in sessions:
            await s.close()
    assert sum(r.created for r in regs) == 1
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(User.id)).where(User.id == 7009)) == 1
        assert await db.scalar(select(func.count(Referral.id)).where(Referral.referred_id == 7009)) == 1


async def test_pin_failure_does_not_break_the_message(e2e, monkeypatch, caplog) -> None:
    h, _ = e2e

    async def broken(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("UNIQUE constraint failed: pinned_admins.username")

    monkeypatch.setattr("app.bot.pinned.svc.on_user", broken)
    h.user(7010, "Дима", username=SENIOR)
    with caplog.at_level(logging.WARNING):
        await h.send(7010, "/start")
    assert texts.ERROR_USER not in h.texts(7010) and h.texts(7010)
    assert any("не удалось закрепить админа" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


# ================================================================ 2. журнал действий
async def test_action_log_lines_are_human_readable_and_without_secrets(e2e, config, caplog) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    h.users[A2] = h.users[A2].model_copy(update={"username": None})
    with caplog.at_level(logging.INFO, logger=ACTIONS):
        await h.send(A1, "/start")
        await h.send(A2, "/start")
        await h.send(A2, M["balance"])
        await h.click(A2, texts.BALANCE_HISTORY_BTN)
        await h.send(A2, "/promo AEGIS-ABCD-2345")
        await h.send(A2, "секретный текст условия")
        await h.send(A2, photo="ph-1")
        await h.send(A2, document=("doc-1", "task.pdf", b"%PDF-1.4 " * 300))
    lines = actions(caplog)
    assert "без username (id 2002) | команда /start" in lines
    assert "без username (id 2002) | нажал «💰 Баланс»" in lines
    assert f"без username (id 2002) | нажал «{texts.BALANCE_HISTORY_BTN}»" in lines
    assert "без username (id 2002) | команда /promo (с параметром)" in lines
    assert any(re.fullmatch(r"без username \(id 2002\) \| ввёл промокод AEG-\*\*\*\*-45: .+", x) for x in lines)
    assert "без username (id 2002) | отправил текст (23 символа)" in lines
    assert "без username (id 2002) | отправил фото" in lines
    assert any(x.startswith("без username (id 2002) | отправил файл (PDF, ") for x in lines)
    blob = "\n".join(lines)
    assert "ABCD" not in blob and "2345" not in blob and "секретный" not in blob

    formatter = RedactingFormatter([config.bot_token])
    record = next(r for r in caplog.records if r.name == ACTIONS)
    assert LINE_RE.match(formatter.format(record)), formatter.format(record)
    leak = logging.LogRecord(ACTIONS, logging.INFO, __file__, 1, f"токен {config.bot_token}", (), None)
    assert config.bot_token not in formatter.format(leak)


async def test_action_log_orders_payments_bonuses_and_admins(sessionmaker, caplog) -> None:
    with caplog.at_level(logging.INFO, logger=ACTIONS):
        async with sessionmaker() as db:
            user = await make_user(db, 2002)
            s = await load_settings(db)
            await ledger.credit_bonus(db, user, 5000, source="admin", kind="admin_credit", ttl_days=30,
                                      now=now_utc())
            await ledger.credit_rub(db, user, 12345, "admin_credit")
            log_admin(db, A1, "give_money", "user", 2002, amount=12345, comment="за решение", code="AEGIS-ABCD-2345")
            await db.commit()
    lines = actions(caplog)
    assert "@user2002 (id 2002) | бонусы +50 ₽ (от админа)" in lines
    assert any(x.startswith("@user2002 (id 2002) | баланс +123,45") for x in lines)
    admin = next(x for x in lines if x.startswith(f"без username (id {A1}) | админ: начислил деньги"))
    assert "пользователь id 2002" in admin and "123,45" in admin
    assert "за решение" not in admin and "ABCD" not in admin


def test_network_noise_is_one_warning_without_traceback() -> None:
    flt = NetworkNoiseFilter()
    try:
        raise TimeoutError("Request timeout")
    except TimeoutError as e:
        import sys
        record = logging.LogRecord("aiogram.dispatcher", logging.ERROR, __file__, 1, "Failed to fetch updates - %s: %s",
                                   ("TelegramNetworkError", e), sys.exc_info())
    assert flt.filter(record)
    assert record.levelno == logging.WARNING and record.exc_info is None
    assert "Request timeout" in record.getMessage()
    sleep = logging.LogRecord("aiogram.dispatcher", logging.WARNING, __file__, 1, "Sleep for %f seconds", (1.0,), None)
    assert not flt.filter(sleep)


# ================================================================ 3. «📎 Скинуть любую задачу»
async def send_any_task(h, uid: int, *, accept_terms: bool = True) -> int:  # noqa: ANN001
    await h.send(uid, M["order"])
    if h.last_text(uid).startswith(texts.DRAFT_SCREEN.split("\n")[0]):
        await h.click(uid, texts.DRAFT_RESTART)
        await h.click(uid, texts.DRAFT_RESTART)
    await h.click(uid, texts.ANY_TASK_BTN)
    assert h.last_text(uid).startswith("📎 <b>Любая задача</b>")
    await h.send(uid, photo=f"any-ph-{uid}-{len(h.texts(uid))}")
    await h.send(uid, document=(f"any-doc-{uid}-{len(h.texts(uid))}", "task.pdf", b"%PDF-1.4"))
    await h.send(uid, "Найти предел, нужно к пятнице")
    assert h.last_text(uid).startswith("✅ Получили:")
    await h.click(uid, texts.ANY_DONE_BTN)
    if accept_terms and any(texts.TERMS_ACCEPT_BTN in row for row in kb_rows(h, uid)):
        await h.click(uid, texts.TERMS_ACCEPT_BTN)
    return int(re.search(r"Задача #(\d+) отправлена на оценку", h.last_text(uid)).group(1))


async def admin_quote(h, oid: int, rub: str) -> None:  # noqa: ANN001
    from app.bot.callbacks import AdminCb

    await h.click_data(A1, AdminCb(s="o", a="quote", id=oid).pack())
    await h.send(A1, rub)


async def test_any_task_from_condition_to_quote_payment_and_delivery(e2e, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")

    await h.send(A2, M["order"])
    assert texts.ANY_TASK_LINE in h.last_text(A2)
    rows = kb_rows(h, A2)
    assert rows[-2] == [texts.ANY_TASK_BTN] and texts.BTN_CANCEL in rows[-1]  # над «Отмена»

    await h.click(A2, texts.ANY_TASK_BTN)
    await h.click(A2, texts.ANY_DONE_BTN)  # пусто: просим сначала прислать условие
    assert texts.ANY_NEED_CONTENT in h.alerts()
    oid = await send_any_task(h, A2)
    order = await get(sessionmaker, Order, oid)
    assert (order.subject, order.status, order.price_final) == ("any", "estimate", None)
    assert "Найти предел" in order.comment
    async with sessionmaker() as db:
        n = await db.scalar(select(func.count(OrderFile.id)).where(OrderFile.order_id == oid))
    assert n == 2

    # админам: уведомление с кнопками и строка во «📥 Входящих»
    assert any(t.startswith(f"📎 <b>Задача на оценку #{oid}</b>") for t in h.texts(A1))
    h.find_button(A1, texts.QUOTE_BTN)
    h.find_button(A1, texts.ANY_REJECT_BTN)
    await h.send(A1, "/admin")
    await h.click(A1, "📥 Входящие")
    assert f"#{oid}" in h.last_text(A1) and "📎" in h.last_text(A1)

    await h.send(A2, M["my_orders"])
    h.find_button(A2, f"#{oid} · {texts.STATUS['estimate']}")  # статус виден в списке «📦 Мои заказы»

    await admin_quote(h, oid, "сколько-то")
    assert texts.QUOTE_BAD in h.last_text(A1)
    await h.send(A1, "350")
    quote = next(t for t in reversed(h.texts(A2)) if "Мы оценили задачу" in t)
    assert quote.startswith(f"💰 <b>Мы оценили задачу: {fmt_rub(35000)}</b>")
    h.find_button(A2, f"{texts.PAY_BTN} · {fmt_rub(35000)}")
    h.find_button(A2, texts.QUOTE_DECLINE_BTN)
    order = await get(sessionmaker, Order, oid)
    assert (order.status, order.price_final, order.quoted_by) == ("quoted", 35000, A1)

    await h.send(A2, M["my_orders"])
    h.find_button(A2, f"#{oid} · {texts.STATUS['quoted']}")  # статус виден в списке «📦 Мои заказы»

    await user_pays(h, A2, 35000)
    assert texts.PAY_DONE_ORDER.format(id=oid) in h.texts(A2)
    order = await get(sessionmaker, Order, oid)
    assert (order.status, order.paid_invoice) == ("paid", 35000)
    await h.send(A2, M["my_orders"])
    h.find_button(A2, f"#{oid} · {texts.STATUS['paid']}")  # статус виден в списке «📦 Мои заказы»

    await admin_deliver(h, oid)  # дальше обычный путь: в работу → решение → клиенту
    assert texts.ORDER_DONE.format(id=oid) in h.texts(A2)
    assert (await get(sessionmaker, Order, oid)).status == "done"


async def test_any_task_bonuses_and_balance_pay_part(e2e, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    async with sessionmaker() as db:
        user = await db.get(User, A2)
        await ledger.credit_bonus(db, user, 30000, source="admin", kind="admin_credit", ttl_days=30, now=now_utc())
        await ledger.credit_rub(db, user, 10000, "admin_credit")
        await db.commit()
    oid = await send_any_task(h, A2)
    await admin_quote(h, oid, "400")
    await user_pays(h, A2, 40000)
    order = await get(sessionmaker, Order, oid)
    # бонусами не больше 50% цены, потом баланс, остаток счётом
    assert (order.status, order.paid_bonus, order.paid_balance, order.paid_invoice) == ("paid", 20000, 10000, 10000)


async def test_any_task_decline_and_reject_with_reason(e2e, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")

    oid = await send_any_task(h, A2)
    await admin_quote(h, oid, "200")
    await h.click(A2, texts.QUOTE_DECLINE_BTN)
    await h.click(A2, texts.QUOTE_DECLINE_YES)
    assert texts.QUOTE_DECLINED.format(id=oid) in h.alerts()
    assert (await get(sessionmaker, Order, oid)).status == "cancelled"
    assert any(t.startswith("✖️") and f"#{oid}" in t for t in h.texts(A1))

    oid2 = await send_any_task(h, A2)
    from app.bot.callbacks import AdminCb

    await h.click_data(A1, AdminCb(s="o", a="reject", id=oid2).pack())
    assert h.last_text(A1).startswith(f"❌ <b>Отказать по задаче #{oid2}</b>")
    await h.send(A1, "Такие задачи не берём")
    assert texts.ANY_REJECTED.format(id=oid2, reason="\n\nПричина: Такие задачи не берём") in h.texts(A2)
    order = await get(sessionmaker, Order, oid2)
    assert order.status == "rejected" and order.price_final is None


async def test_quote_needs_estimate_status(sessionmaker) -> None:
    async with sessionmaker() as db:
        await make_user(db, A2)
        s = await load_settings(db)
        order = await make_order(db, s, A2)  # обычный заказ на проверке
        try:
            await svc.quote(db, order.id, 35000, now_utc(), A1)
        except svc.OrderError:
            pass
        else:  # pragma: no cover
            raise AssertionError("цену назначили не «📎 Любой задаче»")


# ================================================================ 4. тексты без длинных тире
def test_no_long_dashes_in_app_texts() -> None:
    bad = []
    for path in sorted(APP.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "\u2014" in line or "\u2013" in line:
                bad.append(f"{path.relative_to(APP)}:{n}: {line.strip()[:80]}")
    assert not bad, "\n".join(bad)


def test_texts_module_strings_have_no_dashes_even_as_escapes() -> None:
    """Пользовательские строки texts.py: ни «—», ни «–» (даже записанных как \\u2014)."""
    for name in dir(texts):
        value = getattr(texts, name)
        values = value.values() if isinstance(value, dict) else value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            if isinstance(v, str):
                assert "\u2014" not in v and "\u2013" not in v, name


async def test_main_screens_have_bold_titles(e2e) -> None:
    h, _ = e2e
    h.user(A2, "Борис")
    await h.send(A2, "/start")
    for key in ("order", "balance", "my_orders", "invite", "help"):
        await h.send(A2, M[key])
        text = h.last_text(A2)
        assert "<b>" in text.split("\n", 1)[0], (key, text[:80])
        assert "\u2014" not in text and "\u2013" not in text


# ================================================================ 5. цены
async def test_fixed_prices_without_urgency_and_notes(sessionmaker) -> None:
    async with sessionmaker() as db:
        await make_user(db, A2)
        s = await load_settings(db)
        assert (s.price_nach, s.price_calc) == (20000, 40000)
        assert not s.urgency_for("nach") and not s.urgency_for("calc") and s.urgency_for("explain")
        assert not s.notes_discount_nach and not s.notes_discount_calc
        nach = await make_order(db, s, A2, subject="nach", tasks=3, deadline_in=timedelta(hours=3))
        calc = await make_order(db, s, A2, subject="calc", tasks=2, deadline_in=timedelta(hours=3))
        assert (nach.price_auto, nach.urgency_pct) == (60000, 100)
        assert (calc.price_auto, calc.urgency_pct) == (80000, 100)
        with_notes = await make_order(db, s, A2, lecture=True, practice=True)
        assert (with_notes.lecture_status, with_notes.practice_status) == ("none", "none")
        assert with_notes.price_auto == 20000


async def test_prices_screen_texts(e2e) -> None:
    h, _ = e2e
    h.user(A2, "Борис")
    await h.send(A2, "/start")
    await h.send(A2, M["order"])
    step = h.last_text(A2)
    assert f"📐 Начерталка: <b>{fmt_rub(20000)}</b> за задачу" in step
    assert f"<b>{fmt_rub(40000)}</b> за задачу" in step
    for gone in ("−50%", "-50%", "2+ дня без наценки", "за 2+ дня"):
        assert gone not in step
    await h.send(A2, M["help"])
    await h.click(A2, "⏰ Сроки и цены")
    prices = h.last_text(A2)
    assert f"<b>{fmt_rub(20000)}</b>" in prices and "конспект" not in prices.lower()


async def test_deadline_does_not_change_nach_price_but_explain_keeps_urgency(sessionmaker) -> None:
    async with sessionmaker() as db:
        await make_user(db, A2)
        s = await load_settings(db)
        now = now_utc()
        draft = await svc.create_draft(db, A2, svc.EXPLAIN, now)
        await svc.add_file(db, draft.id, "condition", file_type="photo", tg_file_id="mat")
        draft.task_count, draft.comment, draft.deadline_at = 1, "Объясни эпюры", now + timedelta(hours=5)
        await db.commit()
        explain = await svc.submit_order(db, draft.id, s, now)
        assert explain.urgency_pct == s.urgency_lt24_pct > 100


def test_upgrade_0005_to_0006_resets_prices_but_keeps_orders(tmp_path: Path) -> None:
    db = tmp_path / "aegis.db"
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db}"), "0005")
    now = "2026-11-01 10:00:00"
    con = sqlite3.connect(db)
    con.execute("INSERT INTO users (id, username, first_name, created_at, source, ref_code, balance_rub,"
                " balance_bonus, is_priority, priority_auto, free_month_tasks_left, free_month_auto, is_banned,"
                " bot_blocked, remind_tomorrow) VALUES (30, 'vera', 'Вера', ?, 'direct', 'ref30', 0, 0, 0, 0, 0, 0,"
                " 0, 0, 0)", (now,))
    con.execute("INSERT INTO orders (id, user_id, status, subject, task_count, lecture_status, practice_status,"
                " price_per_task, urgency_pct, lecture_discount_pct, practice_discount_pct, discount_pct, free_tasks,"
                " price_auto, price_final, paid_bonus, paid_balance, paid_invoice, is_test, had_dispute, redo_count,"
                " created_at, updated_at) VALUES (7, 30, 'awaiting_payment', 'nach', 2, 'approved', 'none', 30000,"
                " 150, 25, 0, 25, 0, 67500, 67500, 0, 0, 0, 0, 0, 0, ?, ?)", (now, now))
    for key, value in (("price_nach", "30000"), ("notes_discount_nach", "true"), ("terms_text", '"Мои условия"')):
        con.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)", (key, value, now))
    con.commit()
    con.close()

    upgrade_head(f"sqlite+aiosqlite:///{db}")
    con = sqlite3.connect(db)
    row = con.execute("SELECT status, price_per_task, urgency_pct, price_final FROM orders WHERE id = 7").fetchone()
    assert row == ("awaiting_payment", 30000, 150, 67500)  # созданные заказы не пересчитываем
    keys = {k for (k,) in con.execute("SELECT key FROM settings")}
    assert "price_nach" not in keys and "notes_discount_nach" not in keys and "terms_text" in keys
    cols = {c[1] for c in con.execute("PRAGMA table_info(orders)")}
    assert {"quoted_at", "quoted_by"} <= cols
    con.close()


# ================================================================ 6. «💸 Вывести» в балансе
async def test_balance_has_four_buttons_and_withdraw_screen(e2e) -> None:
    h, _ = e2e
    h.user(A2, "Борис")
    await h.send(A2, "/start")
    await h.send(A2, M["balance"])
    assert kb_rows(h, A2) == [[texts.BALANCE_TOPUP_BTN, texts.BALANCE_HISTORY_BTN],
                              [texts.BALANCE_BONUSES_BTN, texts.BALANCE_WITHDRAW_BTN]]
    assert texts.BALANCE_WITHDRAW_BTN == "💸 Вывести"
    await h.click(A2, texts.BALANCE_WITHDRAW_BTN)
    assert h.last_text(A2) == ("💸 Вывод скоро появится. Пока рублями на балансе можно оплатить заказы в боте.")
    assert kb_rows(h, A2) == [["← Назад"]]
    await h.click(A2, "← Назад")
    assert kb_rows(h, A2)[1] == [texts.BALANCE_BONUSES_BTN, texts.BALANCE_WITHDRAW_BTN]


# ================================================================ 7. прочее
def test_no_promo_codes_in_repository() -> None:
    root = APP.parent
    assert not (root / "promo_codes.txt").exists()
    assert not list((root / "migrations" / "versions").glob("0005*promo*codes*"))
    code = re.compile(r"AEGIS-[A-Z0-9]{4}-[A-Z0-9]{4}")
    for path in [*(root / "migrations" / "versions").glob("*.py"), *APP.rglob("*.py"), *root.glob("*.txt")]:
        found = set(code.findall(path.read_text(encoding="utf-8"))) - {"AEGIS-XXXX-XXXX", "AEGIS-ABCD-2345"}
        assert not found, (path.name, sorted(found)[:3])  # только примеры формата, настоящих кодов нет
    assert "promo_codes" in (root / ".gitignore").read_text(encoding="utf-8")


def test_version_is_2_4() -> None:
    root = APP.parent
    assert "2.4" in (root / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## ", 2)[1][:20]
