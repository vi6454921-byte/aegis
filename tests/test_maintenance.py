"""Фоновые задачи и обслуживание: напоминание и сгорание бонусов, очистка тестовых данных, бэкап."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import scheduler, texts
from app.db.models import BonusLot, Order, Payment, Transaction, User
from app.services import payments as pay
from app.services.backup import make_backup
from app.services.ledger import credit_bonus, credit_rub, lock_user
from app.services.maintenance import clear_test_data, integrity_issues, test_data_counts
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc
from tests.fakebot import make_bot
from tests.helpers import invoiced_order, make_user

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_bot(monkeypatch):
    monkeypatch.setattr(scheduler, "SEND_PAUSE", 0)
    return make_bot()


def sent_texts(fake, uid: int) -> list[str]:  # noqa: ANN001
    return [m.text for m, _ in fake.log if type(m).__name__ == "SendMessage" and m.chat_id == uid]


async def test_bonus_reminder_once_then_burn(session, sessionmaker, fake_bot) -> None:
    bot, fake = fake_bot
    t0 = now_utc()
    user = await make_user(session, 5001, now=t0)
    lot = await credit_bonus(session, user, 3000, source="admin", kind="admin_adjust", ttl_days=30, now=t0)
    await session.commit()
    expires = lot.expires_at

    assert await scheduler.remind_bonuses(bot, sessionmaker, now=expires - timedelta(days=10)) == {}
    assert await scheduler.remind_bonuses(bot, sessionmaker, now=expires - timedelta(days=2)) == {5001: 3000}
    assert await scheduler.remind_bonuses(bot, sessionmaker, now=expires - timedelta(days=1)) == {}  # один раз
    assert any(fmt_rub(3000) in t for t in sent_texts(fake, 5001))

    assert await scheduler.burn_bonuses(bot, sessionmaker, now=expires - timedelta(minutes=1)) == {}
    assert await scheduler.burn_bonuses(bot, sessionmaker, now=expires + timedelta(minutes=1)) == {5001: 3000}
    assert await scheduler.burn_bonuses(bot, sessionmaker, now=expires + timedelta(days=1)) == {}
    assert texts.BONUS_EXPIRED.format(amount=fmt_rub(3000)) in sent_texts(fake, 5001)
    async with sessionmaker() as db:
        assert (await db.get(User, 5001)).balance_bonus == 0
        assert (await db.get(BonusLot, lot.id)).expired
        assert await integrity_issues(db) == []


async def test_remind_open_only_when_intake_open(session, sessionmaker, fake_bot) -> None:
    bot, fake = fake_bot
    user = await make_user(session, 5002)
    user.remind_tomorrow = True
    await session.commit()
    assert await scheduler.remind_open(bot, sessionmaker) == 1
    assert await scheduler.remind_open(bot, sessionmaker) == 0  # флаг сброшен
    assert len(sent_texts(fake, 5002)) == 1


async def test_clear_test_data_keeps_real_money(session, s, config) -> None:
    user = await make_user(session, 5003)
    await credit_rub(session, user, 7000, "admin_adjust", comment="реальные деньги")
    await session.commit()
    order = await invoiced_order(session, s, user.id)  # 200 ₽, оплачиваем: 70 ₽ с баланса + 130 ₽ тестовым счётом
    payment = await pay.start_order_payment(session, order.id, user.id, s, now_utc())
    assert payment.provider == "test" and payment.plan_balance == 7000
    assert (await pay.confirm_payment(session, payment.id, s, now_utc())).status == "order_paid"
    user = await lock_user(session, user.id)
    assert user.balance_rub == 0
    assert (await test_data_counts(session))["orders"] == 1

    counts = await clear_test_data(session, config, s, admin_id=1001, now=now_utc())
    assert counts["orders"] == 1 and counts["payments"] == 1
    session.expire_all()
    assert await session.scalar(select(func.count(Order.id))) == 0
    assert await session.scalar(select(func.count(Payment.id))) == 0
    kinds = (await session.execute(select(Transaction.kind, Transaction.amount))).all()
    assert kinds == [("admin_adjust", 7000)]
    assert (await session.get(User, 5003)).balance_rub == 7000  # потраченное на тестовый заказ вернулось
    assert await integrity_issues(session) == []


async def test_backup_is_valid_sqlite(session, config) -> None:
    await make_user(session, 5004)
    path = await make_backup(config)
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        con.close()


def test_restore_script_roundtrip(tmp_path: Path, config) -> None:
    env = {"PATH": "/usr/bin:/bin", "BOT_TOKEN": "123456:TEST", "ADMIN_IDS": "1001",
           "DATABASE_URL": config.database_url, "DATA_DIR": str(config.data_dir)}
    run = lambda *a: subprocess.run([sys.executable, *a], cwd=ROOT, env=env, capture_output=True, text=True,  # noqa: E731
                                    timeout=60)
    made = run("scripts/make_backup.py")
    assert made.returncode == 0, made.stdout + made.stderr
    db = Path(config.database_url.split("///", 1)[1])
    con = sqlite3.connect(db)
    con.execute("DELETE FROM settings")
    con.execute("INSERT INTO users (id, first_name, ref_code, source, balance_rub, balance_bonus, is_banned, "
                "bot_blocked, is_priority, priority_auto, free_month_auto, free_month_tasks_left, remind_tomorrow, "
                "created_at, last_seen_at) VALUES (77, 'после бэкапа', 'zzzz', 'direct', 0, 0, 0, 0, 0, 0, 0, 0, 0, "
                "'2025-01-01 00:00:00', '2025-01-01 00:00:00')")
    con.commit()
    con.close()
    restored = run("scripts/restore_backup.py", "latest", "--yes")
    assert restored.returncode == 0, restored.stdout + restored.stderr
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM users WHERE id = 77").fetchone()[0] == 0
    finally:
        con.close()
    assert list(config.backups_dir.glob("before-restore-*.db"))
    checked = run("scripts/check_integrity.py")
    assert checked.returncode == 0, checked.stdout + checked.stderr
