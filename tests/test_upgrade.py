"""Обновление базы прошлых версий (v1 — миграция 0001, v2, 2.1 — 0003, 2.2 — 0004) с живыми данными → head
без потерь.

Новые столбцы получают значения по умолчанию, старые тексты по умолчанию заменяются новыми («мы»),
а то, что админ менял сам, остаётся как было.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

from alembic import command
from sqlalchemy import select

from app import texts
from app.db.engine import make_engine, make_sessionmaker
from app.db.migrate import ROOT, alembic_config, upgrade_head
from app.db.models import Referral, Ticket, User
from app.services.settings import load_settings
from app.services import tickets
from app.utils.timeutil import now_utc

NOW = "2025-01-01 10:00:00.000000"


def _old_defaults(name: str = "0002_v2_admins_tickets_channel.py") -> dict:
    path = ROOT / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location("m" + name[:4], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.OLD_DEFAULTS


def _make_v1_db(db: Path) -> None:
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db}"), "0001")
    con = sqlite3.connect(db)
    users = [(1, "anya", "Аня", None, "ref1", 0, 7000, 0), (2, "boris", "Борис", 1, "ref2", 15000, 0, 1)]
    for uid, username, name, referrer, code, rub, bonus, banned in users:
        con.execute(
            "INSERT INTO users (id, username, first_name, created_at, source, referrer_id, ref_code, balance_rub,"
            " balance_bonus, is_priority, priority_auto, free_month_tasks_left, free_month_auto, is_banned,"
            " bot_blocked, remind_tomorrow) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, ?, 0, 0)",
            (uid, username, name, NOW, "ref" if referrer else "direct", referrer, code, rub, bonus, banned))
    con.execute("INSERT INTO referrals (referrer_id, referred_id, created_at, rewarded, reward_amount,"
                " reward_is_test, revoked_count) VALUES (1, 2, ?, 1, 5000, 0, 0)", (NOW,))
    old = _old_defaults()
    settings = {"welcome_text": old["welcome_text"],  # не меняли → заменится новым текстом
                "referral_bonus": old["referral_bonus"],  # не меняли → новое значение по умолчанию
                "terms_text": "Мои условия",  # меняли → остаётся
                "price_nach": 25000}
    for key, value in settings.items():
        con.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), NOW))
    con.commit()
    con.close()


def test_upgrade_from_v1_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "aegis.db"
    _make_v1_db(db)
    upgrade_head(f"sqlite+aiosqlite:///{db}")
    upgrade_head(f"sqlite+aiosqlite:///{db}")  # повторный запуск ничего не ломает

    con = sqlite3.connect(db)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005"
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    keys = {k for (k,) in con.execute("SELECT key FROM settings")}
    assert keys == {"terms_text", "price_nach"}
    con.close()

    async def check() -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{db}")
        try:
            sm = make_sessionmaker(engine)
            async with sm() as session:
                anya, boris = await session.get(User, 1), await session.get(User, 2)
                assert (anya.balance_rub, anya.balance_bonus, anya.username) == (0, 7000, "anya")
                assert (boris.balance_rub, boris.referrer_id, boris.is_banned) == (15000, 1, True)
                assert (boris.phone, boris.appeals_count, boris.channel_bonus_amount) == (None, 0, 0)
                assert boris.tutorial_done_at is None and boris.channel_status is None
                ref = await session.scalar(select(Referral).where(Referral.referred_id == 2))
                assert ref.rewarded and ref.reward_amount == 5000
                assert (ref.sub_status, ref.sub_reward_amount, ref.sub_notified) == ("none", 0, False)

                s = await load_settings(session)
                assert s.welcome_text == texts.DEFAULT_WELCOME and "мы" in texts.DEFAULT_WELCOME.lower()
                assert s.terms_text == "Мои условия" and s.price_nach == 25000
                assert s.referral_bonus == 0 and s.channel == "@aegis_samara"

                # новые таблицы рабочие: забаненный из старой базы может обжаловать бан
                now = now_utc()
                ticket, created = await tickets.open_or_append(session, boris.id, "appeal", now)
                tickets.add_in(session, ticket, "text", "Прошу разбанить", 10, now)
                await session.commit()
                assert created and (await session.get(Ticket, ticket.id)).kind == "appeal"
                assert [m.text for m in await tickets.messages(session, ticket.id)] == ["Прошу разбанить"]
        finally:
            await engine.dispose()

    asyncio.run(check())


def _welcome_after_upgrade_from_v2(tmp_path: Path, welcome: str) -> str:
    db = tmp_path / "aegis.db"
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db}"), "0002")
    con = sqlite3.connect(db)
    con.execute("INSERT INTO settings (key, value, updated_at) VALUES ('welcome_text', ?, ?)",
                (json.dumps(welcome, ensure_ascii=False), NOW))
    con.commit()
    con.close()
    upgrade_head(f"sqlite+aiosqlite:///{db}")

    async def load() -> str:
        engine = make_engine(f"sqlite+aiosqlite:///{db}")
        try:
            async with make_sessionmaker(engine)() as session:
                return (await load_settings(session)).welcome_text
        finally:
            await engine.dispose()

    return asyncio.run(load())


def test_upgrade_from_v2_replaces_untouched_welcome_with_gift_links(tmp_path: Path) -> None:
    old = _old_defaults("0003_gift_links.py")["welcome_text"]
    assert "кнопки ниже" in old
    assert _welcome_after_upgrade_from_v2(tmp_path, old) == texts.DEFAULT_WELCOME
    assert "{gifts}" in texts.DEFAULT_WELCOME


def test_upgrade_from_v2_keeps_custom_welcome(tmp_path: Path) -> None:
    assert _welcome_after_upgrade_from_v2(tmp_path, "Мой привет, {name}!") == "Мой привет, {name}!"


def test_upgrade_from_v21_keeps_admins_and_money_and_adds_task_tables(tmp_path: Path) -> None:
    """2.1 → 2.2 (0003 → 0004): админы, деньги и настройки на месте, у старых админов pinned=False,
    новые таблицы заданий и закреплённых админов пустые и рабочие, журнал денег сходится."""
    from app.db.models import Admin, PinnedAdmin, ProfileClaim, PromoPost, PromoPostReport
    from app.services.maintenance import integrity_issues

    db = tmp_path / "aegis.db"
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db}"), "0003")
    con = sqlite3.connect(db)
    for uid, username, name, rub in ((10, "vera", "Вера", 25000), (11, "gleb", "Глеб", 0)):
        con.execute(
            "INSERT INTO users (id, username, first_name, created_at, source, referrer_id, ref_code, balance_rub,"
            " balance_bonus, is_priority, priority_auto, free_month_tasks_left, free_month_auto, is_banned,"
            " bot_blocked, remind_tomorrow) VALUES (?, ?, ?, ?, 'direct', NULL, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0)",
            (uid, username, name, NOW, f"ref{uid}", rub))
    con.execute("INSERT INTO transactions (user_id, account, amount, kind, is_test, created_at)"
                " VALUES (10, 'rub', 25000, 'admin_credit', 0, ?)", (NOW,))
    con.execute("INSERT INTO admins (user_id, level, added_by, added_at) VALUES (11, 1, 10, ?)", (NOW,))
    con.execute("INSERT INTO settings (key, value, updated_at) VALUES ('price_nach', '30000', ?)", (NOW,))
    con.commit()
    con.close()

    upgrade_head(f"sqlite+aiosqlite:///{db}")
    upgrade_head(f"sqlite+aiosqlite:///{db}")  # повторный запуск ничего не ломает
    con = sqlite3.connect(db)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005"
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT user_id, level, added_by, pinned FROM admins").fetchall() == [(11, 1, 10, 0)]
    for table in ("pinned_admins", "profile_claims", "promo_posts", "promo_post_reports"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
    con.close()

    async def check() -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{db}")
        try:
            async with make_sessionmaker(engine)() as session:
                vera = await session.get(User, 10)
                assert (vera.balance_rub, vera.username) == (25000, "vera")
                assert (await session.get(Admin, 11)).pinned is False
                assert await integrity_issues(session) == []
                s = await load_settings(session)
                assert s.price_nach == 30000  # своё значение из 2.1 осталось
                assert (s.profile_task_reward, s.post_base_reward, s.post_views_step) == (10000, 20000, 100)
                assert (s.post_views_reward, s.post_views_cap, s.admin_give_confirm_over) == (10000, 0, 1000000)

                now = now_utc()  # новые таблицы рабочие
                session.add(PinnedAdmin(username="badvinq", user_id=11, source="username", bound_at=now))
                session.add(ProfileClaim(user_id=10, status="pending", file_id="f", created_at=now))
                post = PromoPost(user_id=10, platform="tg", post_url="t.me/c/1", status="pending",
                                 created_at=now, last_update_at=now)
                session.add(post)
                await session.flush()
                session.add(PromoPostReport(post_id=post.id, kind="first", status="pending", created_at=now))
                await session.commit()
                assert post.credited == 0
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_upgrade_from_v22_keeps_data_and_adds_promo_codes(tmp_path: Path) -> None:
    """2.2 → 2.3 (0004 → 0005): пользователи, деньги, заказы (и черновик с фото), переписка и шаги сценариев
    на месте; добавлены 45 промокодов — ровно те, что в promo_codes.txt; новые таблицы пустые и рабочие."""
    from app.bot.drafts import draft_info
    from app.db.models import AdminChat, Order, PromoAttempt, StrayMessage
    from app.services import promocodes as pc
    from app.services.maintenance import integrity_issues

    db = tmp_path / "aegis.db"
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db}"), "0004")
    con = sqlite3.connect(db)
    for uid, username, name, rub in ((20, "vera", "Вера", 12345), (21, "gleb", "Глеб", 0)):
        con.execute(
            "INSERT INTO users (id, username, first_name, created_at, source, referrer_id, ref_code, balance_rub,"
            " balance_bonus, is_priority, priority_auto, free_month_tasks_left, free_month_auto, is_banned,"
            " bot_blocked, remind_tomorrow) VALUES (?, ?, ?, ?, 'direct', NULL, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0)",
            (uid, username, name, NOW, f"ref{uid}", rub))
    con.execute("INSERT INTO transactions (user_id, account, amount, kind, is_test, created_at)"
                " VALUES (20, 'rub', 12345, 'admin_credit', 0, ?)", (NOW,))
    order_cols = ("id, user_id, status, subject, task_count, lecture_status, practice_status, lecture_discount_pct,"
                  " practice_discount_pct, discount_pct, free_tasks, paid_bonus, paid_balance, paid_invoice, is_test,"
                  " had_dispute, redo_count, created_at, updated_at")
    con.execute(f"INSERT INTO orders ({order_cols}) VALUES (1, 20, 'draft', 'nach', NULL, 'none', 'none', 0, 0, 0,"
                " 0, 0, 0, 0, 0, 0, 0, ?, ?)", (NOW, NOW))
    con.execute(f"INSERT INTO orders ({order_cols}) VALUES (2, 21, 'review', 'calc', 2, 'none', 'none', 0, 0, 0,"
                " 0, 0, 0, 0, 0, 0, 0, ?, ?)", (NOW, NOW))
    for n in (1, 2):
        con.execute("INSERT INTO order_files (order_id, kind, file_type, tg_file_id, created_at)"
                    " VALUES (1, 'condition', 'photo', ?, ?)", (f"cond{n}", NOW))
    con.execute("INSERT INTO tickets (id, user_id, kind, status, order_id, created_at, updated_at)"
                " VALUES (5, 21, 'order', 'open', 2, ?, ?)", (NOW, NOW))
    con.execute("INSERT INTO ticket_messages (ticket_id, direction, content_type, text, created_at)"
                " VALUES (5, 'in', 'text', 'Когда будет готово?', ?)", (NOW,))
    con.execute("INSERT INTO fsm_states (key, state, data, updated_at) VALUES ('bot:20:20', 'OrderSG:photos',"
                " '{\"order_id\": 1}', ?)", (NOW,))
    con.commit()
    con.close()

    upgrade_head(f"sqlite+aiosqlite:///{db}")
    upgrade_head(f"sqlite+aiosqlite:///{db}")  # повторный запуск ничего не ломает
    con = sqlite3.connect(db)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005"
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT key, state FROM fsm_states").fetchall() == [("bot:20:20", "OrderSG:photos")]
    for table in ("promo_attempts", "stray_messages", "admin_chats"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
    con.close()

    async def check() -> None:
        engine = make_engine(f"sqlite+aiosqlite:///{db}")
        try:
            async with make_sessionmaker(engine)() as session:
                vera = await session.get(User, 20)
                assert (vera.balance_rub, vera.username, vera.unlimited_forever, vera.unlimited_until) == (
                    12345, "vera", False, None)
                assert await integrity_issues(session) == []
                draft, review = await session.get(Order, 1), await session.get(Order, 2)
                assert (draft.status, draft.unlimited, draft.draft_warned_at) == ("draft", False, None)
                assert (review.status, review.task_count, review.unlimited) == ("review", 2, False)
                info = await draft_info(session, 20)
                assert info is not None and info.photos == 2  # черновик из 2.2 с фото — «📝 Продолжить»
                assert [m.text for m in await tickets.messages(session, 5)] == ["Когда будет готово?"]
                s = await load_settings(session)
                assert (s.unlimited_bypass_limit, s.draft_ttl_days, s.stray_timeout_minutes) == (False, 7, 10)

                codes = await pc.all_codes(session)
                assert len(codes) == 45 and sum(c.kind == "forever" for c in codes) == 35
                assert all(c.days == 30 for c in codes if c.kind == "days")
                assert all(c.used_by is None and c.revoked_at is None for c in codes)
                from_file = pc.parse_txt((ROOT / "promo_codes.txt").read_text(encoding="utf-8"))
                assert from_file == pc.parse_txt(pc.export_txt(codes))

                now = now_utc()  # коды из файла работают, новые таблицы — тоже
                res = await pc.activate(session, 21, from_file["Навсегда (35)"][0], now)
                assert res.ok and res.status == "forever"
                assert (await session.get(Order, 2, populate_existing=True)).unlimited  # заказ на проверке — бесплатный
                assert (await pc.activate(session, 20, "AEGIS-ZZZZ-ZZZZ", now)).status == "invalid"
                session.add(StrayMessage(user_id=20, chat_id=20, message_id=1, content_type="text", text="?",
                                         status="pending", created_at=now))
                session.add(AdminChat(admin_id=21, user_id=20, order_id=1, started_at=now, sent=0))
                await session.commit()
                assert len((await session.execute(select(PromoAttempt))).scalars().all()) == 2
        finally:
            await engine.dispose()

    asyncio.run(check())
