"""Новое в 2.3: безлимит по промокодам, черновики заказа без потерь, «Это задание?» для сообщений вне
оформления, «📥 Входящие» у админа, режим чата админа с клиентом по заказу и маскировка токена в логах."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from urllib.parse import quote

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InlineKeyboardButton
from sqlalchemy import func, select, update

from app import scheduler, texts
from app.bot.callbacks import AdminCb
from app.bot.handlers import stray
from app.bot.middlewares import AntiFloodMiddleware
from app.bot.render import paid_line
from app.config import Config
from app.db.migrate import ROOT
from app.db.models import AdminChat, AdminLog, Order, OrderFile, Payment, PromoCode, Ticket, TicketMessage, User
from app.logging_setup import MASK, setup_logging
from app.services import orders as svc
from app.services import promocodes as pc
from app.services import roles, stats, tickets
from app.services.settings import load_settings
from app.utils.money import fmt_rub, parse_rub
from app.utils.timeutil import full_date, now_utc
from tests.conftest import BOT_USERNAME
from tests.fakebot import Harness
from tests.helpers import invoiced_order, make_order, make_user
from tests.scenario import A1, A2, set_settings, wizard_order

M = texts.MAIN_MENU
A3 = 3003


# ---------------------------------------------------------------- помощники
async def condition_photos(sessionmaker, user_id: int) -> int:  # noqa: ANN001
    async with sessionmaker() as db:
        return int(await db.scalar(select(func.count(OrderFile.id)).join(Order, Order.id == OrderFile.order_id)
                                   .where(Order.user_id == user_id, Order.status == "draft",
                                          OrderFile.kind == "condition")) or 0)


def reply_menu(h, uid: int) -> list[str]:  # noqa: ANN001
    """Кнопки последнего нижнего меню (reply-клавиатуры), которое бот прислал пользователю."""
    for method, _ in reversed(h.messages(uid)):
        kb = getattr(method, "reply_markup", None)
        if kb is not None and hasattr(kb, "keyboard"):
            return [b.text for row in kb.keyboard for b in row]
    return []


def buttons(h, uid: int) -> list[str]:  # noqa: ANN001
    """Inline-кнопки, которые сейчас на экране у uid."""
    return [b.text for _, kb in h.screen(uid) if kb for row in kb.inline_keyboard for b in row]


def calls_to(h, uid: int, *methods: str, since: int = 0) -> list:  # noqa: ANN001
    return [m for m, _ in h.fake.log[since:]
            if type(m).__name__ in methods and str(getattr(m, "chat_id", "")) == str(uid)]


async def start(h, uid: int, name: str) -> None:  # noqa: ANN001
    h.user(uid, name)
    await h.send(uid, "/start")


async def free_code(sessionmaker, kind: str = "forever", skip: int = 0) -> str:  # noqa: ANN001
    async with sessionmaker() as db:
        codes = (await db.execute(select(PromoCode.code).where(
            PromoCode.kind == kind, PromoCode.used_by.is_(None), PromoCode.revoked_at.is_(None))
            .order_by(PromoCode.id))).scalars().all()
    return codes[skip]


async def get_user(sessionmaker, uid: int) -> User:  # noqa: ANN001
    async with sessionmaker() as db:
        return await db.get(User, uid)


async def open_promo_hub(h) -> None:  # noqa: ANN001
    await h.send(A1, "/admin")
    await h.click_data(A1, AdminCb(s="g").pack())  # 🔐 Управление
    await h.click(A1, texts.PC_HUB_BTN)


async def order_with_chat(h) -> int:  # noqa: ANN001
    """Клиент оформил заказ, админ нажал «✉️ Ответить» под уведомлением — режим чата включён."""
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    oid = await wizard_order(h, A2, notes=False)
    await h.click_data(A1, AdminCb(s="c", a="o", id=oid).pack())
    return oid


def restart(h: Harness, config: Config, sessionmaker) -> Harness:  # noqa: ANN001
    """«Перезапуск бота»: новый диспетчер (FSM и режим чата — в БД), тот же фейковый Telegram."""
    from app.main import build_dispatcher

    dp = build_dispatcher(config, sessionmaker, BOT_USERNAME)
    for mw in dp.update.outer_middleware:
        if isinstance(mw, AntiFloodMiddleware):
            mw.burst = 10**6
    fresh = Harness(dp, h.bot, h.fake)
    fresh.users = h.users
    return fresh


# ---------------------------------------------------------------- баг: пропадали фото и заказы
async def test_repro_draft_photos_survive_leaving_the_wizard(e2e, sessionmaker) -> None:
    """Воспроизведение бага: начал заказ → прислал фото → нажал другие кнопки → прислал ещё фото.
    Раньше второе фото терялось («Не поняли»), а «Отмена» удаляла черновик целиком."""
    h, _ = e2e
    h.user(A2, "Борис")
    await h.send(A2, "/start")
    await h.send(A2, M["order"])
    await h.click(A2, texts.SUBJECTS["nach"])
    await h.send(A2, photo="cond-1")
    assert await condition_photos(sessionmaker, A2) == 1

    for label in (M["balance"], M["my_orders"], M["help"], M["gifts"]):  # ушёл в другие разделы
        await h.send(A2, label)
    await h.send(A2, "/start")
    assert await condition_photos(sessionmaker, A2) == 1  # черновик на месте
    menu = reply_menu(h, A2)
    assert texts.DRAFT_MENU_BTN.format(n=1) in menu, menu

    await h.send(A2, photo="cond-2")  # фото вне мастера — не теряется: «Это задание?»
    assert texts.STRAY_BTN_ORDER in [b.text for _, kb in h.screen(A2) if kb for row in kb.inline_keyboard for b in row]
    await h.click(A2, texts.STRAY_BTN_ORDER)
    assert await condition_photos(sessionmaker, A2) == 2

    await h.click(A2, texts.BTN_CANCEL)  # «✖️ Отмена» — черновик не удаляется
    assert await condition_photos(sessionmaker, A2) == 2
    await h.send(A2, texts.DRAFT_MENU_BTN.format(n=2))
    await h.click(A2, texts.DRAFT_CONTINUE)
    assert "Уже прислано: 2" in h.last_text(A2)


# ---------------------------------------------------------------- промокоды: активация
async def test_promo_forever_via_menu_button_any_spelling(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    code = await free_code(sessionmaker, "forever")
    await h.send(A2, M["promo"])
    assert "Промокод на безлимит" in h.last_text(A2)
    await h.send(A2, code.removeprefix("AEGIS-").lower().replace("-", " "))  # без «AEGIS», строчными, с пробелом
    assert h.last_text(A2).startswith(texts.PROMO_OK_FOREVER)

    user = await get_user(sessionmaker, A2)
    async with sessionmaker() as db:
        row = await db.scalar(select(PromoCode).where(PromoCode.code == code))
    assert user.unlimited_forever and user.unlimited_until is None and user.unlimited_code_id == row.id
    assert row.used_by == A2 and row.used_at is not None

    await h.send(A2, M["balance"])  # статус виден в «💰 Баланс»
    assert texts.UNLIMITED_STATUS_FOREVER in h.last_text(A2)
    await h.send(A2, f"/promo {code}")  # свой же код ещё раз — не ошибка
    assert h.last_text(A2) == texts.PROMO_MINE
    async with sessionmaker() as db:
        assert (await pc.fails_in_window(db, A2, now_utc()))[0] == 0


async def test_promo_30_days(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    before = now_utc()
    await h.send(A2, f"/promo {await free_code(sessionmaker, 'days')}")
    user = await get_user(sessionmaker, A2)
    assert not user.unlimited_forever
    assert timedelta(days=30) <= user.unlimited_until - before <= timedelta(days=30, minutes=1)
    assert h.last_text(A2).startswith(texts.PROMO_OK_UNTIL.format(date=full_date(user.unlimited_until)))
    await h.send(A2, M["balance"])
    assert texts.UNLIMITED_STATUS_UNTIL.format(date=full_date(user.unlimited_until)) in h.last_text(A2)


async def test_promo_code_is_one_time_even_when_entered_at_once(sessionmaker) -> None:
    async with sessionmaker() as db:
        for uid in (2002, 2003, 2004):
            await make_user(db, uid)
    code = await free_code(sessionmaker)
    now = now_utc()

    async def attempt(uid: int) -> tuple[int, pc.Activation]:
        async with sessionmaker() as db:
            return uid, await pc.activate(db, uid, code, now)

    results = dict(await asyncio.gather(*(attempt(uid) for uid in (2002, 2003, 2004))))
    winners = [uid for uid, res in results.items() if res.ok]
    assert len(winners) == 1
    assert sorted(res.status for res in results.values()) == ["forever", "used", "used"]
    async with sessionmaker() as db:
        assert (await db.scalar(select(PromoCode).where(PromoCode.code == code))).used_by == winners[0]
        active = [uid for uid in (2002, 2003, 2004) if pc.is_active(await db.get(User, uid), now)]
        assert active == winners
        loser = next(uid for uid in (2002, 2003, 2004) if uid not in winners)
        assert (await pc.activate(db, loser, code, now)).status == "used"
        assert (await pc.activate(db, winners[0], code, now)).status == "mine"


async def test_promo_race_lost_in_database_changes_nothing(sessionmaker, monkeypatch) -> None:
    """Два процесса бота: код прочитан как свободный, но другой процесс успел записать его раньше.
    Условный UPDATE (used_by IS NULL) не перезаписывает владельца, и безлимит не включается."""
    async with sessionmaker() as db:
        await make_user(db, 2002)
        await make_user(db, 2003)
    code = await free_code(sessionmaker)
    now = now_utc()
    original = pc._lock_user  # noqa: SLF001

    async def other_process_wins(session, user_id: int):  # noqa: ANN001, ANN202
        async with sessionmaker() as other:
            await other.execute(update(PromoCode).where(PromoCode.code == code).values(used_by=2003, used_at=now))
            await other.commit()
        return await original(session, user_id)

    monkeypatch.setattr(pc, "_lock_user", other_process_wins)
    async with sessionmaker() as db:
        assert (await pc.activate(db, 2002, code, now)).status == "used"
    async with sessionmaker() as db:
        assert (await db.scalar(select(PromoCode).where(PromoCode.code == code))).used_by == 2003
        assert not pc.is_active(await db.get(User, 2002), now)


async def test_promo_refused_while_unlimited_active_and_forever_replaces_30_days(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    await start(h, A3, "Вера")
    f1, f2 = await free_code(sessionmaker, "forever", 0), await free_code(sessionmaker, "forever", 1)
    d1, d2 = await free_code(sessionmaker, "days", 0), await free_code(sessionmaker, "days", 1)

    await h.send(A2, f"/promo {f1}")  # безлимит навсегда → новые коды не принимаем и не списываем
    for code in (d1, f2):
        await h.send(A2, f"/promo {code}")
        assert h.last_text(A2) == texts.PROMO_HAS_FOREVER

    await h.send(A3, f"/promo {d1}")  # 30 дней → второй 30-дневный не продлевает
    until = (await get_user(sessionmaker, A3)).unlimited_until
    await h.send(A3, f"/promo {d2}")
    assert h.last_text(A3) == texts.PROMO_HAS_DAYS.format(date=full_date(until))
    assert (await get_user(sessionmaker, A3)).unlimited_until == until
    await h.send(A3, f"/promo {f2}")  # а «навсегда» заменяет 30 дней
    assert h.last_text(A3).startswith(texts.PROMO_OK_FOREVER + texts.PROMO_OK_REPLACED)
    vera = await get_user(sessionmaker, A3)
    assert vera.unlimited_forever and vera.unlimited_until is None

    async with sessionmaker() as db:
        used = dict((await db.execute(select(PromoCode.code, PromoCode.used_by)
                                      .where(PromoCode.code.in_((f1, f2, d1, d2))))).all())
        assert used == {f1: A2, f2: A3, d1: A3, d2: None}  # отказ код не тратит
        assert (await pc.fails_in_window(db, A2, now_utc()))[0] == 0  # и попыткой перебора не считается


async def test_unlimited_30_days_reminder_then_expiry(e2e, sessionmaker, config) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await h.send(A2, f"/promo {await free_code(sessionmaker, 'days')}")
    until = (await get_user(sessionmaker, A2)).unlimited_until
    async with sessionmaker() as db:  # заказ, отправленный во время безлимита, бесплатный и после его конца
        order = await make_order(db, await load_settings(db), A2)
    assert order.unlimited

    watch = scheduler.unlimited_watch
    assert await watch(h.bot, config, sessionmaker, until - timedelta(days=5)) == (0, 0)
    assert await watch(h.bot, config, sessionmaker, until - timedelta(days=2)) == (1, 0)
    assert h.last_text(A2).startswith(f"⏰ Безлимит закончится {full_date(until)}, осталось")
    assert await watch(h.bot, config, sessionmaker, until - timedelta(days=1)) == (0, 0)  # напоминаем один раз
    assert await watch(h.bot, config, sessionmaker, until + timedelta(minutes=1)) == (0, 1)
    assert h.last_text(A2) == texts.PROMO_ENDED
    assert await watch(h.bot, config, sessionmaker, until + timedelta(hours=2)) == (0, 0)

    user = await get_user(sessionmaker, A2)
    assert not user.unlimited_active(until + timedelta(minutes=1)) and user.unlimited_until is None
    async with sessionmaker() as db:
        paid = await svc.issue_invoice(db, order.id, await load_settings(db), until + timedelta(minutes=2), A1)
    assert (paid.status, paid.price_final) == ("paid", 0)
    async with sessionmaker() as db:  # новый заказ — уже по обычной цене
        fresh = await make_order(db, await load_settings(db), A2)
    assert not fresh.unlimited


async def test_promo_bruteforce_5_wrong_codes_per_hour(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    for i, tail in enumerate("23456", start=1):
        await h.send(A2, f"/promo AEGIS-ZZZZ-ZZZ{tail}")
        if i < 5:
            assert h.last_text(A2) == texts.PROMO_INVALID.format(left=5 - i)
    assert h.last_text(A2).startswith("⏸ Слишком много неверных попыток")

    code = await free_code(sessionmaker)  # даже верный код в паузе не принимаем — и не тратим
    await h.send(A2, f"/promo {code}")
    assert h.last_text(A2).startswith("⏸ Слишком много неверных попыток")
    await h.send(A2, code)
    assert h.last_text(A2).startswith("⏸ Слишком много неверных попыток")
    assert not (await get_user(sessionmaker, A2)).unlimited_forever
    async with sessionmaker() as db:
        assert (await db.scalar(select(PromoCode).where(PromoCode.code == code))).used_by is None
        assert await pc.blocked_for(db, A2, now_utc()) > 0
        assert await pc.blocked_for(db, A2, now_utc() + timedelta(hours=1, minutes=1)) == 0
        res = await pc.activate(db, A2, code, now_utc() + timedelta(hours=1, minutes=1))  # через час — можно
    assert res.status == "forever"


async def test_plain_words_are_not_promo_attempts(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    for word in ("привет", "AEGIS", "Задача 1-2", "1234-5678"):
        await h.send(A2, word)
    async with sessionmaker() as db:
        assert (await pc.fails_in_window(db, A2, now_utc()))[0] == 0
    await h.send(A2, "AEGIS-ZZZZ-ZZZ2")  # похоже на код — это попытка
    assert h.last_text(A2) == texts.PROMO_INVALID.format(left=4)


# ---------------------------------------------------------------- безлимит: бесплатные заказы
async def test_free_order_by_unlimited_without_invoice(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    before = await get_user(sessionmaker, A2)
    await h.send(A2, f"/promo {await free_code(sessionmaker)}")
    oid = await wizard_order(h, A2, notes=False)
    assert "У тебя безлимит, счёт не придёт" in h.last_text(A2)
    alert = next(t for t in reversed(h.texts(A1)) if texts.ADMIN_ORDER_UNLIMITED in t)
    assert f"#{oid}" in alert

    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    labels = buttons(h, A1)
    assert texts.ADMIN_ACCEPT_UNLIMITED_BTN in labels and "✏️ Цена" not in labels
    assert not any(label.startswith("💳 Выставить счёт") for label in labels)
    assert texts.ORDER_UNLIMITED_LINE in h.last_text(A1)
    await h.click(A1, texts.ADMIN_ACCEPT_UNLIMITED_BTN)

    async with sessionmaker() as db:
        order = await db.get(Order, oid)
        assert (order.status, order.price_final, order.unlimited, order.paid_invoice) == ("paid", 0, True, 0)
        assert await db.scalar(select(func.count(Payment.id)).where(Payment.order_id == oid)) == 0
        entry = await db.scalar(select(AdminLog).where(AdminLog.action == "issue_invoice",
                                                       AdminLog.target_id == str(oid)))
        assert json.loads(entry.details)["method"] == "promo_unlimited"
        user = await db.get(User, A2)
        assert (user.balance_rub, user.balance_bonus) == (before.balance_rub, before.balance_bonus)
    line = paid_line(order)
    assert "промокод" in line and line.endswith("0 ₽</b>")
    assert texts.INVOICE_UNLIMITED.format(id=oid) in h.texts(A2)
    assert not any(texts.PAY_BTN in label for label in buttons(h, A2))  # кнопки оплаты не было

    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    assert "промокод (безлимит), <b>0 ₽</b>" in h.last_text(A1)
    assert "🛠 Взять в работу" in buttons(h, A1)  # заказ в очереди


async def test_unlimited_explain_is_free_and_notes_need_no_review(sessionmaker) -> None:
    await set_settings(sessionmaker, notes_discount_nach=True)  # v2.4: по умолчанию скидок за конспекты нет
    async with sessionmaker() as db:
        await make_user(db, 2002)
        await pc.grant(db, 2002, None, A1, now_utc())
        s = await load_settings(db)
        now = now_utc()
        draft = await svc.create_draft(db, 2002, svc.EXPLAIN, now)
        await svc.add_file(db, draft.id, "condition", file_type="photo", tg_file_id="mat")
        draft.task_count, draft.comment, draft.deadline_at = 1, "Объясни эпюры", now + timedelta(days=2)
        await db.commit()
        explain = await svc.submit_order(db, draft.id, s, now)
        assert explain.unlimited
        assert (await svc.issue_invoice(db, explain.id, s, now, A1)).price_final == 0
        with_notes = await make_order(db, s, 2002, lecture=True, practice=True)
        assert with_notes.lecture_status == "pending"
        paid = await svc.issue_invoice(db, with_notes.id, s, now, A1)  # конспекты проверять не нужно: 0 ₽
        assert (paid.status, paid.price_final) == ("paid", 0)


async def test_unlimited_orders_count_in_daily_limit_unless_bypass_setting(sessionmaker) -> None:
    async with sessionmaker() as db:
        for uid in (2002, 2003, 2004):
            await make_user(db, uid)
        await pc.grant(db, 2002, None, A1, now_utc())
        assert (await load_settings(db)).unlimited_bypass_limit is False  # по умолчанию — как обычные заказы
    await set_settings(sessionmaker, max_tasks_per_day=1)
    async with sessionmaker() as db:
        s = await load_settings(db)
        assert (await make_order(db, s, 2002)).unlimited  # безлимитный занял единственное место
        with pytest.raises(svc.IntakeClosed):
            await make_order(db, s, 2003)
        with pytest.raises(svc.IntakeClosed):
            await make_order(db, s, 2002)

    await set_settings(sessionmaker, unlimited_bypass_limit=True)
    async with sessionmaker() as db:
        s = await load_settings(db)
        assert (await make_order(db, s, 2003)).status == "review"  # безлимитные места у других не занимают
        with pytest.raises(svc.IntakeClosed):
            await make_order(db, s, 2004)
        assert (await make_order(db, s, 2002)).unlimited  # а сами проходят сверх лимита
        await set_settings(sessionmaker, intake_mode="closed")
        with pytest.raises(svc.IntakeClosed):  # ручное закрытие приёма — для всех
            await make_order(db, await load_settings(db), 2002)


async def test_stats_count_unlimited_orders_apart_from_revenue(sessionmaker) -> None:
    async with sessionmaker() as db:
        await make_user(db, 2002)
        await pc.grant(db, 2002, None, A1, now_utc())
        s = await load_settings(db)
        free = await invoiced_order(db, s, 2002)
        assert (free.status, free.price_final, free.unlimited) == ("paid", 0, True)  # в очереди, 0 ₽
        report = await stats.admin_report(db, now_utc())
    assert "♾ Безлимит (не выручка): активен у 1 (навсегда 1, на срок 0)" in report
    assert "заказов по безлимиту 1, из них принято в очередь 1" in report
    assert f"• Поступило по счетам: {fmt_rub(0)} | {fmt_rub(0)}" in report  # в выручку не идёт
    assert f"• Оплачено заказов рублями: {fmt_rub(0)} | {fmt_rub(0)}" in report
    assert "→ оплата 0 (0%)" in report  # и в воронке это не «оплата»
    assert "🎟 Промокоды: свободно 45 · активировано 0 · отозвано 0" in report


async def test_activation_puts_awaiting_payment_orders_in_queue(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    async with sessionmaker() as db:
        order = await invoiced_order(db, await load_settings(db), A2)
    assert order.status == "awaiting_payment" and order.price_final > 0
    await h.send(A2, f"/promo {await free_code(sessionmaker)}")
    assert texts.PROMO_OK_PAID.format(ids=f"#{order.id}").strip() in h.last_text(A2)
    async with sessionmaker() as db:
        moved = await db.get(Order, order.id)
    assert (moved.status, moved.price_final, moved.unlimited) == ("paid", 0, True)
    assert any(f"заказ #{order.id} теперь бесплатный" in t for t in h.texts(A1))


# ---------------------------------------------------------------- промокоды в админке
async def test_admin_revokes_code_and_client_is_notified(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    code = await free_code(sessionmaker)
    await h.send(A2, f"/promo {code}")
    oid = await wizard_order(h, A2, notes=False)  # ждёт проверки — пока бесплатный

    await open_promo_hub(h)
    assert "Всего 45 · свободно 44 · активировано 1 · отозвано 0" in h.last_text(A1)
    await h.click(A1, texts.PC_FIND_BTN)
    await h.send(A1, code.lower().replace("-", " "))
    assert code in h.last_text(A1) and "активирован" in h.last_text(A1)
    await h.click(A1, texts.PC_REVOKE_BTN)
    assert "это текущий безлимит" in h.last_text(A1)
    await h.click(A1, "⛔ Да, отозвать")
    assert h.last_text(A1).startswith(f"⛔ Промокод {code} отозван.")

    assert texts.PROMO_REVOKED_NOTICE in h.texts(A2)
    async with sessionmaker() as db:
        row = await db.scalar(select(PromoCode).where(PromoCode.code == code))
        assert row.revoked_at is not None and row.revoked_by == A1
        assert not pc.is_active(await db.get(User, A2), now_utc())
        assert not (await db.get(Order, oid)).unlimited  # ещё не принятый заказ снова платный
        logged = await db.scalar(select(AdminLog).where(AdminLog.action == "promo_revoke"))
        assert json.loads(logged.details)["unlimited_removed"] is True
    await h.send(A2, f"/promo {code}")
    assert h.last_text(A2) == texts.PROMO_REVOKED.format(left=4)


async def test_admin_grants_and_removes_unlimited_by_hand(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await open_promo_hub(h)
    await h.click(A1, texts.PC_GRANT_BTN)
    await h.send(A1, f"@user{A2}")
    await h.click(A1, texts.PC_GRANT_DAYS_BTN.format(days=30))
    until = (await get_user(sessionmaker, A2)).unlimited_until
    assert until is not None and h.last_text(A2) == texts.UNLIMITED_GRANTED_UNTIL.format(date=full_date(until))
    assert (await get_user(sessionmaker, A2)).unlimited_by == A1

    await h.click(A1, texts.PC_GRANT_FOREVER_BTN)  # продлить → навсегда
    assert (await get_user(sessionmaker, A2)).unlimited_forever
    assert h.last_text(A2) == texts.UNLIMITED_GRANTED_FOREVER

    await h.click(A1, texts.PC_REMOVE_BTN)
    await h.click(A1, "🚫 Да, снять")
    user = await get_user(sessionmaker, A2)
    assert not user.unlimited_forever and user.unlimited_until is None
    assert h.last_text(A2) == texts.UNLIMITED_REMOVED_NOTICE
    async with sessionmaker() as db:
        actions = (await db.execute(select(AdminLog.action).where(AdminLog.action.like("unlimited_%"))
                                    .order_by(AdminLog.id))).scalars().all()
    assert actions == ["unlimited_grant", "unlimited_grant", "unlimited_remove"]


async def test_level1_admin_has_no_promo_codes(e2e, sessionmaker, config) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await start(h, A3, "Вера")
    async with sessionmaker() as db:
        await roles.set_level(db, config, A3, 1, A1, now_utc())
        code_id = await db.scalar(select(PromoCode.id).order_by(PromoCode.id))
    await h.send(A3, "/admin")
    assert not {texts.MANAGE_BTN, texts.PC_HUB_BTN} & set(buttons(h, A3))
    for a, target, v in (("", 0, ""), ("dl", 0, ""), ("code", code_id, ""), ("revoke_go", code_id, ""),
                         ("gen_go", 5, "forever"), ("users", 0, ""), ("grant_go", A2, "forever"),
                         ("remove_go", A2, "")):  # старые или подделанные кнопки
        data = AdminCb(s="pc", a=a, id=target, v=v).pack()
        await h.press(A3, h.last_message(A3), InlineKeyboardButton(text="•", callback_data=data))
        assert h.alerts()[-1] == texts.ADMIN_L2_ONLY, a
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(PromoCode.id))) == 45
        assert await db.scalar(select(func.count(PromoCode.id)).where(PromoCode.revoked_at.is_not(None))) == 0
        assert not (await db.get(User, A2)).unlimited_forever
        assert await db.scalar(select(func.count(AdminLog.id)).where(AdminLog.action.like("promo_%"))) == 0


async def test_promo_codes_txt_matches_database(e2e, sessionmaker) -> None:
    """v2.4: файла с кодами в проекте нет; выгрузка из админки = коды в базе (в тестах их 45, см. conftest)."""
    h, _ = e2e
    assert not (ROOT / "promo_codes.txt").exists()
    async with sessionmaker() as db:
        codes = await pc.all_codes(db)
    file_sections = pc.parse_txt(pc.export_txt(codes))
    assert {k: len(v) for k, v in file_sections.items()} == {"Навсегда (35)": 35, "30 дней (10)": 10}
    assert set(file_sections["Навсегда (35)"]) == {c.code for c in codes if c.kind == "forever"}
    assert set(file_sections["30 дней (10)"]) == {c.code for c in codes if c.kind == "days" and c.days == 30}
    everything = [c for v in file_sections.values() for c in v]
    assert len(set(everything)) == len(codes) == 45 and all(pc.normalize(c) == c for c in everything)

    # «📄 Скачать промокоды (txt)» — из базы: активированные на месте, отозванные не выгружаются
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await h.send(A2, f"/promo {codes[1].code}")
    async with sessionmaker() as db:
        await pc.revoke(db, codes[0].id, A1, now_utc())
    await open_promo_hub(h)
    await h.click(A1, texts.PC_DOWNLOAD_BTN)
    doc = h.sent_documents(A1)[-1].document
    assert doc.filename == "promo_codes.txt"
    got = pc.parse_txt(doc.data.decode("utf-8"))
    assert got == {"Навсегда (34)": file_sections["Навсегда (35)"][1:], "30 дней (10)": file_sections["30 дней (10)"]}

    await h.click(A1, texts.PC_GENERATE_BTN)  # выпуск новых: файл только с новыми кодами
    await h.click(A1, texts.PC_GEN_FOREVER_BTN)
    await h.click(A1, "5", exact=True)
    new_doc = h.sent_documents(A1)[-1].document
    new_codes = pc.parse_txt(new_doc.data.decode("utf-8"))["Навсегда (5)"]
    async with sessionmaker() as db:
        assert {c.code for c in await pc.all_codes(db)} >= set(new_codes)
        assert len(await pc.all_codes(db)) == 50
    assert all(pc.normalize(c) == c for c in new_codes) and not set(new_codes) & set(everything)


# ---------------------------------------------------------------- черновики и альбомы
async def test_old_draft_is_warned_then_deleted(e2e, sessionmaker, config) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    await h.send(A2, M["order"])
    await h.click(A2, texts.SUBJECTS["nach"])
    await h.send(A2, photo="old-cond")
    now = now_utc()
    cleanup = scheduler.drafts_cleanup
    assert await cleanup(h.bot, config, sessionmaker, now + timedelta(days=5)) == (0, 0)
    assert await cleanup(h.bot, config, sessionmaker, now + timedelta(days=6, hours=1)) == (1, 0)
    assert h.last_text(A2).startswith("⏳ Черновик заказа (фото: 1) без движения уже 6 дн.")
    assert texts.DRAFT_CONTINUE in buttons(h, A2)
    assert await cleanup(h.bot, config, sessionmaker, now + timedelta(days=6, hours=2)) == (0, 0)  # один раз
    assert await condition_photos(sessionmaker, A2) == 1
    assert await cleanup(h.bot, config, sessionmaker, now + timedelta(days=7, hours=1)) == (0, 1)
    assert h.last_text(A2) == texts.DRAFT_EXPIRED.format(days=7)
    assert not any(b.startswith(texts.DRAFT_MENU_PREFIX) for b in reply_menu(h, A2))
    async with sessionmaker() as db:
        assert await svc.get_draft(db, A2) is None
        assert await db.scalar(select(func.count(OrderFile.id))) == 0


async def test_overdue_draft_is_warned_first_and_deleted_a_day_later(e2e, sessionmaker, config) -> None:
    """Черновик уже старше срока, а предупреждения не было (бот был выключен, черновик остался с 2.2)."""
    h, _ = e2e
    await start(h, A2, "Борис")
    await h.send(A2, M["order"])
    await h.click(A2, texts.SUBJECTS["nach"])
    await h.send(A2, photo="old-cond")
    async with sessionmaker() as db:
        touched = (await svc.get_draft(db, A2)).updated_at
    later = now_utc() + timedelta(days=30)
    cleanup = scheduler.drafts_cleanup
    assert await cleanup(h.bot, config, sessionmaker, later) == (1, 0)  # сразу не удаляем — предупреждаем
    assert h.last_text(A2).startswith("⏳ Черновик заказа (фото: 1) без движения уже 30 дн., завтра мы его удалим.")
    async with sessionmaker() as db:
        assert (await svc.get_draft(db, A2)).updated_at == touched  # предупреждение — не «движение»
    assert await cleanup(h.bot, config, sessionmaker, later + timedelta(hours=23)) == (0, 0)
    assert await condition_photos(sessionmaker, A2) == 1
    assert await cleanup(h.bot, config, sessionmaker, later + timedelta(days=1)) == (0, 1)
    assert h.last_text(A2) == texts.DRAFT_EXPIRED.format(days=31)


async def test_album_in_wizard_keeps_every_photo_with_one_reply(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    await h.send(A2, M["order"])
    await h.click(A2, texts.SUBJECTS["nach"])
    seen = len(h.texts(A2))
    await h.send_album(A2, [f"alb-{i}" for i in range(6)], group="wizard-album", caption="Задача 3")
    assert await condition_photos(sessionmaker, A2) == 6
    assert h.texts(A2)[seen:] == [texts.STEP_PHOTOS_ACK.format(n=6)]
    await h.send_album(A2, ["alb-0", "alb-1"], group="wizard-album-again")  # тот же альбом ещё раз — без дублей
    assert await condition_photos(sessionmaker, A2) == 6


async def test_album_outside_wizard_one_question_then_whole_album_to_order(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2, "Борис")
    seen = len(h.texts(A2))
    await h.send_album(A2, ["out-1", "out-2", "out-3"], group="stray-album")
    assert [t for t in h.texts(A2)[seen:] if "Это задание?" in t] == [h.texts(A2)[-1]]
    await h.click(A2, texts.STRAY_BTN_ORDER)
    assert await condition_photos(sessionmaker, A2) == 3
    assert any(texts.STRAY_TO_ORDER.format(n=3) in t for t in h.texts(A2)[seen:])
    await h.click(A2, texts.SUBJECTS["nach"])  # предмет не выбран — спросили, фото уже в черновике
    assert "Уже прислано: 3" in h.last_text(A2)


# ---------------------------------------------------------------- «Это задание?» вне оформления
async def test_stray_photo_goes_to_support_after_timeout(e2e, sessionmaker, config) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await h.send(A2, photo="lonely", caption="Что это за задача?")
    assert "Это задание?" in h.last_text(A2) and "10 мин" in h.last_text(A2)
    assert {texts.STRAY_BTN_ORDER, texts.STRAY_BTN_SUPPORT} <= set(buttons(h, A2))
    prompt, old_btn = h.find_button(A2, texts.STRAY_BTN_ORDER)

    now = now_utc()
    assert await stray.flush_stray(h.bot, config, sessionmaker, now + timedelta(minutes=5)) == 0
    assert await stray.flush_stray(h.bot, config, sessionmaker, now + timedelta(minutes=11)) == 1
    assert h.last_text(A2) == texts.STRAY_AUTO
    assert texts.STRAY_BTN_ORDER not in buttons(h, A2)  # кнопки вопроса убрали
    async with sessionmaker() as db:
        ticket = await db.scalar(select(Ticket).where(Ticket.user_id == A2))
        msgs = (await db.execute(select(TicketMessage).where(TicketMessage.ticket_id == ticket.id))).scalars().all()
    assert ticket.kind == "support" and ticket.status == "open"
    assert [(m.direction, m.content_type, m.text) for m in msgs] == [("in", "photo", "Что это за задача?")]
    assert any(texts.STRAY_ADMIN_AUTO.format(minutes=10) in t for t in h.texts(A1))
    assert calls_to(h, A1, "CopyMessage")  # само фото — админам
    assert await stray.flush_stray(h.bot, config, sessionmaker, now + timedelta(minutes=30)) == 0  # без дублей

    await h.press(A2, prompt, old_btn)  # старая кнопка — «уже разобрались», черновик не создаётся
    assert texts.STRAY_DONE_ALREADY in h.alerts()
    assert await condition_photos(sessionmaker, A2) == 0


async def test_stray_text_to_support_by_button_and_timeout_setting(e2e, sessionmaker, config) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await h.send(A2, "Помогите с задачей 5, срок завтра")
    await h.click(A2, texts.STRAY_BTN_SUPPORT)
    assert texts.STRAY_TO_SUPPORT in h.texts(A2)
    assert any(texts.STRAY_ADMIN_NOTE in t and "Помогите с задачей 5" in t for t in h.texts(A1))
    async with sessionmaker() as db:
        rows = (await db.execute(select(TicketMessage.text).join(Ticket).where(Ticket.user_id == A2))).scalars().all()
    assert rows == ["Помогите с задачей 5, срок завтра"]

    await set_settings(sessionmaker, stray_timeout_minutes=3)
    await h.send(A2, document=("stray-doc", "условие.pdf", b"%PDF-1.4"))
    assert "3 мин" in h.last_text(A2)
    assert await stray.flush_stray(h.bot, config, sessionmaker, now_utc() + timedelta(minutes=4)) == 1


# ---------------------------------------------------------------- ✉️ режим чата админа с клиентом
async def test_chat_mode_delivers_every_message_type(e2e, sessionmaker) -> None:
    h, _ = e2e
    oid = await order_with_chat(h)
    assert f"Вы пишете клиенту @user{A2} по заказу №{oid}" in h.last_text(A1)
    assert {texts.CHAT_FINISH_BTN, f"📦 Заказ #{oid}"} <= set(buttons(h, A1))
    head = texts.CHAT_TO_USER.format(mark=texts.CHAT_MARK_ORDER.format(id=oid))

    mark = len(h.fake.log)
    await h.send(A1, "Привет! Уточни, пожалуйста, вариант")
    await h.send(A1, photo="adm-photo", caption="Смотри эпюру")
    await h.send(A1, voice="adm-voice")
    await h.send(A1, video="adm-video")
    await h.send(A1, audio="adm-audio")
    await h.send(A1, document=("adm-html", "решение.html", b"<html><body>ok</body></html>"))
    await h.send(A1, document=("adm-zip", "проект.zip", b"PK\x03\x04zip"))
    await h.send(A1, sticker="adm-sticker")
    await h.send_album(A1, ["adm-a1", "adm-a2", "adm-a3"], group="admin-album")

    sends = calls_to(h, A2, "SendMessage", since=mark)
    copies = calls_to(h, A2, "CopyMessage", since=mark)
    albums = calls_to(h, A2, "CopyMessages", since=mark)
    assert sends[0].text == f"{head}\n\nПривет! Уточни, пожалуйста, вариант"
    assert [m.text for m in sends[1:]] == [head, head]  # пометка над стикером и над альбомом
    assert len(copies) == 7 and [m.caption for m in copies[:2]] == [f"{head}\n\nСмотри эпюру", head]
    assert all(m.caption == head for m in copies[2:6]) and copies[6].caption is None  # у стикера подписи нет
    assert len(albums) == 1 and len(albums[0].message_ids) == 3  # альбом — целиком, одним альбомом
    assert all(m.reply_markup and m.reply_markup.inline_keyboard[0][0].text == texts.SUPPORT_REPLY_BTN
               for m in [sends[0], *copies])

    confirmations = [m.text for m in calls_to(h, A1, "SendMessage", since=mark)
                     if m.text.startswith(texts.CHAT_DELIVERED)]
    assert confirmations == [texts.CHAT_DELIVERED] * 8 + [texts.CHAT_DELIVERED
                                                          + texts.CHAT_DELIVERED_ALBUM.format(n=3)]
    async with sessionmaker() as db:
        rows, total = await tickets.order_thread(db, oid)
        chat = await db.get(AdminChat, A1)
    assert [m.content_type for m in rows] == ["text", "photo", "voice", "video", "audio", "document", "document",
                                              "sticker", "photo", "photo", "photo"]
    assert total == 11 and {m.direction for m in rows} == {"out"} and len({m.ticket_id for m in rows}) == 1
    assert chat.sent == 11


async def test_chat_reports_blocked_client(e2e, sessionmaker) -> None:
    h, _ = e2e
    await order_with_chat(h)
    await h.block_bot(A2)
    await h.send(A1, "Ты тут?")
    assert h.last_text(A1) == texts.CHAT_BLOCKED
    await h.send(A1, photo="to-blocked")
    assert h.last_text(A1) == texts.CHAT_BLOCKED
    async with sessionmaker() as db:
        assert (await db.get(AdminChat, A1)).sent == 0


async def test_chat_mode_survives_restart(e2e, sessionmaker, config) -> None:
    h, _ = e2e
    oid = await order_with_chat(h)
    h2 = restart(h, config, sessionmaker)
    try:
        await h2.send(A1, "Пишу после перезапуска")
        assert h2.last_text(A1) == texts.CHAT_DELIVERED
        assert h2.last_text(A2).endswith("Пишу после перезапуска")
        await h2.send(A1, "/admin")
        assert f"Вы в чате с клиентом @user{A2} (По заказу №{oid})" in h2.last_text(A1)
        await h2.click(A1, texts.CHAT_RETURN_BTN.format(id=oid))
        assert f"Вы пишете клиенту @user{A2} по заказу №{oid}" in h2.last_text(A1)
        assert "Пишу после перезапуска" in h2.last_text(A1)  # последние сообщения переписки — в заголовке
    finally:
        h2.close()


async def test_chat_mode_and_other_admin_buttons(e2e, sessionmaker) -> None:
    h, _ = e2e
    oid = await order_with_chat(h)
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())  # карточка заказа
    await h.click(A1, "✏️ Цена")
    to_client = len(calls_to(h, A2, "SendMessage", "CopyMessage"))
    await h.send(A1, "450")  # идёт во ввод цены, а не клиенту
    async with sessionmaker() as db:
        assert (await db.get(Order, oid)).price_override == parse_rub("450")
    assert len(calls_to(h, A2, "SendMessage", "CopyMessage")) == to_client
    await h.send(A1, "А теперь снова клиенту")  # следующее — снова клиенту
    assert h.last_text(A1) == texts.CHAT_DELIVERED
    assert h.last_text(A2).endswith("А теперь снова клиенту")

    await h.send(A1, "/admin")  # другие разделы админки режим не сбивают
    await h.click_data(A1, AdminCb(s="in").pack())
    await h.send(A1, photo="after-inbox")
    assert h.last_text(A1) == texts.CHAT_DELIVERED

    await h.click(A1, texts.CHAT_FINISH_BTN)
    assert h.last_text(A1) == texts.CHAT_FINISHED.format(mark=f"По заказу №{oid}", n=2)
    async with sessionmaker() as db:
        assert await db.get(AdminChat, A1) is None
    to_client = len(calls_to(h, A2, "SendMessage", "CopyMessage"))
    await h.send(A1, "это уже не клиенту")
    assert len(calls_to(h, A2, "SendMessage", "CopyMessage")) == to_client
    await h.send(A1, "/admin")
    assert texts.CHAT_RETURN_BTN.format(id=oid) not in buttons(h, A1)


async def test_chat_switches_to_another_order(e2e, sessionmaker) -> None:
    h, _ = e2e
    first = await order_with_chat(h)
    await start(h, A3, "Вера")
    second = await wizard_order(h, A3, notes=False)
    await h.click_data(A1, AdminCb(s="c", a="o", id=second).pack())
    assert texts.CHAT_SWITCHED.format(old=f"По заказу №{first}") in h.last_text(A1)
    await h.send(A1, "Вере")
    assert h.last_text(A3).endswith("Вере") and not h.last_text(A2).endswith("Вере")


async def test_client_swipe_reply_from_any_step_lands_in_order_conversation(e2e, sessionmaker) -> None:
    h, _ = e2e
    oid = await order_with_chat(h)
    await h.send(A1, "Пришли, пожалуйста, фото условия крупнее")
    team_msg = h.last_message(A2)

    await h.send(A2, M["order"])  # клиент тем временем начал новый заказ — мастер ждёт фото
    await h.click(A2, texts.SUBJECTS["calc"])
    key = StorageKey(bot_id=h.bot.id, chat_id=A2, user_id=A2)
    step = await h.dp.storage.get_state(key)
    assert step is not None
    await h.send(A2, photo="bigger", reply_to=team_msg)  # свайп-ответ из мастера
    await h.send(A2, "Вот, крупнее", reply_to=team_msg)
    assert await h.dp.storage.get_state(key) == step  # мастер не сбился
    assert await condition_photos(sessionmaker, A2) == 0  # фото ушло в переписку, а не в новый черновик

    await h.click(A2, texts.SUPPORT_REPLY_BTN)  # кнопкой «💬 Ответить» под сообщением — туда же
    await h.send(A2, "И ещё вопрос по сроку")

    async with sessionmaker() as db:
        rows, _ = await tickets.order_thread(db, oid)
    assert [(m.direction, m.content_type, m.text or None) for m in rows] == [
        ("out", "text", "Пришли, пожалуйста, фото условия крупнее"), ("in", "photo", None),
        ("in", "text", "Вот, крупнее"), ("in", "text", "И ещё вопрос по сроку")]
    assert len({m.ticket_id for m in rows}) == 1
    assert sum(f"Заказ #{oid}:" in t for t in h.texts(A1)) >= 3  # админам — с пометкой заказа


async def test_order_card_shows_conversation(e2e, sessionmaker) -> None:
    h, _ = e2e
    oid = await order_with_chat(h)
    await h.send(A1, "Какой у тебя вариант?")
    await h.send(A2, "Вариант 7", reply_to=h.last_message(A2))
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    card = h.last_text(A1)
    assert "💬 Клиент ждёт ответа: «Вариант 7»" in card
    thread = card[card.index("💬 <b>Переписка</b> (последние 2 из 2):"):]
    assert thread.index("🛡 Аня · ") < thread.index("Какой у тебя вариант?") < thread.index(
        f"{texts.CHAT_FROM_CLIENT} · ") < thread.index("Вариант 7")
    await h.click(A1, texts.CHAT_HISTORY_BTN)
    assert h.last_text(A1).startswith(f"💬 <b>Переписка · По заказу №{oid}</b> (2 сообщ.)")


async def test_admin_swipe_reply_on_order_alert_goes_to_client(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    oid = await wizard_order(h, A2, notes=False)
    want = AdminCb(s="c", a="o", id=oid).pack()
    alert = next(msg for _, msg in reversed(h.messages(A1)) if msg.reply_markup and any(
        b.callback_data == want for row in msg.reply_markup.inline_keyboard for b in row))
    await h.send(A1, "Фото нечёткое — пересними, пожалуйста", reply_to=alert)
    assert h.last_text(A1) == f"{texts.CHAT_DELIVERED} · По заказу №{oid}"
    assert h.last_text(A2).endswith("Фото нечёткое — пересними, пожалуйста")
    async with sessionmaker() as db:
        assert await db.get(AdminChat, A1) is None  # свайп не включает режим чата
        rows, _ = await tickets.order_thread(db, oid)
    assert [m.text for m in rows] == ["Фото нечёткое — пересними, пожалуйста"]


# ---------------------------------------------------------------- 📥 Входящие
async def test_inbox_lists_new_orders_and_messages_newest_first(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await start(h, A2, "Борис")
    await start(h, A3, "Вера")
    oid = await wizard_order(h, A2, notes=False)
    await h.send(A3, "/support")
    await h.send(A3, "Не приходит решение")

    await h.send(A1, "/admin")
    assert texts.INBOX_TODO.format(n="<b>2</b>") in h.last_text(A1)
    await h.click(A1, texts.INBOX_BTN.format(n=2))
    view = h.last_text(A1)
    assert view.startswith("📥 <b>Входящие</b>: ждут нас 2\n\nСначала новые.")
    assert view.index("Не приходит решение") < view.index(f"Заказ #{oid}")

    async with sessionmaker() as db:  # заказ пришёл позавчера — в «За сегодня» его нет
        await db.execute(update(Order).where(Order.id == oid).values(submitted_at=now_utc() - timedelta(days=2)))
        await db.commit()
    await h.click(A1, texts.INBOX_TODAY_BTN)
    today = h.last_text(A1)
    assert "(за сегодня)" in today and "Не приходит решение" in today and f"Заказ #{oid}" not in today
    await h.click(A1, texts.INBOX_ALL_BTN)
    assert f"Заказ #{oid}" in h.last_text(A1)

    seen = len(h.fake.log)
    await h.click_data(A1, AdminCb(s="in", a="o", id=oid).pack())
    assert calls_to(h, A1, "SendPhoto", "SendMediaGroup", "SendDocument", since=seen)  # сначала фото условия
    assert f"Заказ #{oid}</b>" in h.last_text(A1)  # под ними — карточка новым сообщением

    async with sessionmaker() as db:  # обращение из «Входящих» → «✉️ Ответить» → ответ уходит клиенту
        ticket_id = await db.scalar(select(Ticket.id).where(Ticket.user_id == A3))
    await h.click_data(A1, AdminCb(s="t", a="open", id=ticket_id).pack())
    await h.click(A1, texts.CHAT_BTN)
    assert f"Вы пишете клиенту @user{A3} по обращению №{ticket_id}" in h.last_text(A1)
    await h.send(A1, "Уже отправили повторно")
    assert h.last_text(A1) == texts.CHAT_DELIVERED
    assert h.last_text(A3).endswith("Уже отправили повторно")
    await h.send(A1, "/admin")  # ответили — во «Входящих» остался только заказ
    assert texts.INBOX_BTN.format(n=1) in buttons(h, A1)


async def test_inbox_empty(e2e) -> None:
    h, _ = e2e
    await start(h, A1, "Аня")
    await h.send(A1, "/admin")
    assert "📥 Входящие" in buttons(h, A1)
    await h.click(A1, "📥 Входящие", exact=True)
    assert h.last_text(A1).startswith(texts.INBOX_EMPTY)


# ---------------------------------------------------------------- настройки 2.3
async def test_new_settings_defaults_and_bounds(s) -> None:
    from app.services.settings import parse_value, setting_type

    assert (s.unlimited_bypass_limit, s.draft_ttl_days, s.stray_timeout_minutes) == (False, 7, 10)
    assert setting_type("unlimited_bypass_limit") == "bool"
    assert parse_value("draft_ttl_days", "14") == 14 and parse_value("stray_timeout_minutes", "30") == 30
    for key, bad in (("draft_ttl_days", "0"), ("draft_ttl_days", "91"), ("stray_timeout_minutes", "0")):
        with pytest.raises(ValueError):
            parse_value(key, bad)


# ---------------------------------------------------------------- логи: токен не попадает в файл
def test_logging_masks_bot_token(tmp_path) -> None:
    token = "7012345678:AAFakeToken_ForTests-0123456789abcdef"  # ненастоящий
    other = "123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789"  # чужой/старый токен тоже маскируем
    cfg = Config(_env_file=None, BOT_TOKEN=token, ADMIN_IDS="1", DATA_DIR=str(tmp_path / "data"))
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    try:
        setup_logging(cfg)
        log = logging.getLogger("aegis.test")
        log.info("запрос https://api.telegram.org/bot%s/getMe", token)
        log.warning("в URL-кодировке: %s", quote(token, safe=""))
        log.error("старый токен %s в тексте", other)
        try:
            raise RuntimeError(f"Bad Request: https://api.telegram.org/bot{token}/sendMessage")
        except RuntimeError:
            log.exception("ошибка запроса")
        for handler in root.handlers:
            handler.flush()
        out = (cfg.logs_dir / "aegis.log").read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            if not isinstance(handler, logging.StreamHandler) or hasattr(handler, "baseFilename"):
                handler.close()
        for handler in saved:
            root.addHandler(handler)
        root.setLevel(level)
    assert token not in out and quote(token, safe="") not in out and other not in out
    assert "AAFakeToken" not in out and "ABCdefGHI" not in out
    assert "запрос https://api.telegram.org/bot***/getMe" in out
    assert "в URL-кодировке: ***" in out and "старый токен *** в тексте" in out
    assert out.count(MASK) == 4  # три сообщения + текст исключения в traceback


def test_real_token_lives_only_in_env_file() -> None:
    env = ROOT / ".env"
    token = ""
    if env.exists():
        token = next((line.split("=", 1)[1].strip() for line in env.read_text(encoding="utf-8").splitlines()
                      if line.startswith("BOT_TOKEN=")), "")
    if len(token) < 20:
        pytest.skip("в .env нет токена")
    skip = {"data", "__pycache__", ".pytest_cache", ".git", "venv", ".venv", "node_modules"}
    leaks = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
             if p.is_file() and p.name != ".env" and not skip & set(p.relative_to(ROOT).parts)
             and token.encode() in p.read_bytes()]
    assert leaks == []  # сам токен в сообщение об ошибке не попадает — только пути
