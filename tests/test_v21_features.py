"""v2.1: подарки — только ссылки в тексте сообщений; режим оплаты переключает админ 2 уровня,
при включении звёзд (или перевода) тестовая оплата исчезает полностью, вернуть тест можно."""
from __future__ import annotations

import re

from sqlalchemy import select

from app import texts
from app.bot.callbacks import AdminCb, GiftCb
from app.db.models import AdminLog, BonusLot, Payment, Transaction, User
from app.services import maintenance, paymode
from app.services import payments as paysvc
from app.services.maintenance import integrity_issues
from app.services.settings import load_settings, set_setting
from app.utils.money import fmt_rub, stars_for
from app.utils.timeutil import now_utc
from tests.helpers import make_user
from tests.scenario import A1, A2, GIFT_URLS, admin_invoice, get, gift_hrefs, set_settings, wizard_order
from tests.test_v2_features import last_kb_labels, press_data

A3, A4 = 2003, 2004


def welcome_of(h, uid: int):  # noqa: ANN001, ANN201
    """Последнее приветствие у uid: (текст, клавиатура)."""
    return next((m.text, kb) for m, kb in reversed(h.screen(uid)) if "Привет! Это Аегис" in (m.text or ""))


def labels(h, uid: int) -> list[str]:  # noqa: ANN001
    """Все кнопки, что сейчас на экране uid."""
    return [b.text for _, kb in h.screen(uid) if kb for row in kb.inline_keyboard for b in row]


async def mode(sessionmaker) -> str:  # noqa: ANN001
    async with sessionmaker() as db:
        return (await load_settings(db)).payments_mode


# ---------------------------------------------------------------- подарки
async def test_gifts_are_links_in_text_without_buttons(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A2, "Борис")
    await h.send(A2, "/start")
    text, kb = welcome_of(h, A2)
    assert gift_hrefs(text) == list(GIFT_URLS)  # расчётка, начерталка, матрицы — в этом порядке
    assert texts.GIFTS_WELCOME_TITLE in text and kb is None
    assert not h.sent_documents(A2)

    await h.send(A2, texts.MAIN_MENU["gifts"])
    menu = h.last_text(A2)
    assert menu.startswith(texts.GIFTS_TITLE) and gift_hrefs(menu) == list(GIFT_URLS)
    assert "метод Гаусса" in menu and "Понять, а не зазубрить" in menu and "Крамера, Гаусса и ранга" in menu
    assert set(last_kb_labels(h, A2)) == {texts.CHANNEL_OPEN_BTN, texts.CHANNEL_CHECK_BTN}  # кнопки — только канал

    await press_data(h, A2, GiftCb(gift="nachertalka"))  # кнопка подарка из старого сообщения
    assert gift_hrefs(h.last_text(A2)) == list(GIFT_URLS) and not h.sent_documents(A2)


async def test_custom_welcome_gets_gift_links(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A2, "Борис")
    await set_settings(sessionmaker, welcome_text="Хай, {name}! {stats}.")  # без {gifts} — ссылки добавятся в конце
    await h.send(A2, "/start")
    text = h.texts(A2)[0]
    assert text.startswith("Хай, Борис! Сдано 90 работ") and gift_hrefs(text) == list(GIFT_URLS)
    await set_settings(sessionmaker, welcome_text="{gifts}\nХай, {name}!")
    await h.send(A2, "/start")
    text = [t for t in h.texts(A2) if t.startswith(texts.GIFTS_WELCOME_TITLE)][-1]
    assert text.endswith("Хай, Борис!") and gift_hrefs(text) == list(GIFT_URLS)


# ---------------------------------------------------------------- режим оплаты
async def test_enabling_stars_removes_test_payment_completely(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await set_settings(sessionmaker, topup_bonus_enabled=True, topup_bonus_map={"20000": 3000})
    await h.send(A1, "/start")
    await h.send(A2, "/start")

    # тестовый режим: неоплаченный тестовый счёт по заказу + тестовое пополнение 200 ₽ (+30 ₽ бонусами)
    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")
    assert "🧪 Тестовый режим" in h.last_text(A2)
    await h.send(A2, texts.MAIN_MENU["balance"])
    await h.click(A2, "➕ Пополнить")
    await h.click(A2, fmt_rub(20000))
    await h.click(A2, texts.PAY_TEST_BTN)
    async with sessionmaker() as db:  # и настоящие деньги: 50 ₽ и 10 ₽ бонусами от админа
        s = await load_settings(db)
        await paysvc.admin_adjust(db, A2, "rub", 5000, "компенсация", s, now_utc(), A1)
        await paysvc.admin_adjust(db, A2, "bonus", 1000, "подарок", s, now_utc(), A1)
    user = await get(sessionmaker, User, A2)
    assert (user.balance_rub, user.balance_bonus) == (25000, 4000)

    # админ 2 уровня: кнопка прямо на главном экране → подтверждение с тем, что уберётся
    await h.send(A1, "/admin")
    assert "💳 Оплата: 🧪 тест" in h.last_text(A1)
    await h.click(A1, "⭐ Включить оплату звёздами")
    confirm = h.last_text(A1)
    assert confirm.startswith("⭐ <b>Включить оплату звёздами?</b>")
    assert "неоплаченные тестовые счета отменятся: <b>1</b>" in confirm
    assert f"<b>{fmt_rub(20000)}</b> и <b>{fmt_rub(3000)}</b> бонусами (клиентов: 1)" in confirm
    assert await mode(sessionmaker) == "test"  # пока не подтвердили — ничего не меняется
    await h.click(A1, "✅ Да, включить звёзды")
    done = h.last_text(A1)
    assert done.startswith("✅ Включена оплата звёздами") and "Отменено тестовых счетов: 1" in done
    assert "Сейчас: <b>⭐ Telegram Stars</b>" in done and "🧪 Вернуть тестовую оплату" in last_kb_labels(h, A1)
    assert await mode(sessionmaker) == "stars"

    # тестовые деньги убраны, настоящие — на месте; тестовый счёт отменён
    user = await get(sessionmaker, User, A2)
    assert (user.balance_rub, user.balance_bonus) == (5000, 1000)
    async with sessionmaker() as db:
        statuses = dict((await db.execute(select(Payment.purpose, Payment.status)
                                          .where(Payment.is_test.is_(True)))).all())
        assert statuses == {"order": "cancelled", "topup": "paid"}
        lots = (await db.execute(select(BonusLot.is_test, BonusLot.remaining))).all()
        assert sorted(lots) == [(False, 1000), (True, 0)]
        kinds = (await db.execute(select(Transaction.account, Transaction.amount)
                                  .where(Transaction.kind == paymode.TEST_OFF))).all()
        assert sorted(kinds) == [("bonus", -3000), ("rub", -20000)]
        assert await integrity_issues(db) == []

    # у клиента тестовой оплаты больше нет: старая кнопка «(тест)» не работает и исчезает
    await h.click(A2, texts.PAY_TEST_BTN)
    assert h.alerts()[-1] == texts.PAY_STALE and texts.PAY_TEST_BTN not in labels(h, A2)
    await h.send(A2, texts.MAIN_MENU["balance"])
    await h.click(A2, "📜 История")
    assert texts.TX_KINDS["test_off"] in h.last_text(A2)
    n = len(h.calls("SendInvoice"))
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")  # тот же заказ — теперь счёт в звёздах
    [inv] = h.calls("SendInvoice")[n:]
    stars = stars_for(20000 - 1000 - 5000, "1.75")  # бонусы 10 ₽ и баланс 50 ₽ — настоящие, остальное звёздами
    assert inv.currency == "XTR" and inv.prices[0].amount == stars
    assert texts.PAY_TEST_BTN not in labels(h, A2) and "🧪" not in h.last_text(A2)
    await h.click(A1, "🛡 В админку")
    assert "⭐ Включить оплату звёздами" not in last_kb_labels(h, A1)

    # вернуть тест можно: ⚙️ Настройки → 💳 Оплата → Режим оплаты
    await h.click(A1, "⚙️ Настройки")
    await h.click(A1, "💳 Оплата")
    await h.click(A1, "Режим оплаты: ⭐ Telegram Stars")
    await h.click(A1, "🧪 Вернуть тестовую оплату")
    assert "Уже выставленные настоящие счета (1) остаются в силе" in h.last_text(A1)
    await h.click(A1, "✅ Да, вернуть тест")
    assert h.last_text(A1).startswith("🧪 Включена тестовая оплата") and await mode(sessionmaker) == "test"
    assert await h.pre_checkout(A2, inv.payload, stars)  # выставленный счёт в звёздах по-прежнему оплачивается
    await h.successful_payment(A2, inv.payload, stars, "charge-1")
    assert texts.PAY_DONE_ORDER.format(id=oid) in h.texts(A2)
    async with sessionmaker() as db:
        order_paid = await db.scalar(select(Payment).where(Payment.payload == inv.payload))
        assert order_paid.status == "paid" and not order_paid.is_test
        assert await integrity_issues(db) == []
    assert (await get(sessionmaker, User, A2)).balance_rub == 0

    # снова тест — снова кнопка «(тест)»; и обратно на звёзды без лишних вопросов про тестовые деньги
    oid2 = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid2, 20000)
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")
    assert texts.PAY_TEST_BTN in last_kb_labels(h, A2)
    await h.send(A1, "/admin")
    await h.click(A1, "⭐ Включить оплату звёздами")
    await h.click(A1, "✅ Да, включить звёзды")
    await h.click(A2, texts.PAY_TEST_BTN)
    assert h.alerts()[-1] == texts.PAY_STALE
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []
        await maintenance.clear_test_data(db, config, await load_settings(db), A1, now_utc())
        assert await integrity_issues(db) == []
    user = await get(sessionmaker, User, A2)
    assert (user.balance_rub, user.balance_bonus) == (0, 0)  # настоящие 50 ₽ и 10 ₽ ушли на оплату звёздами


async def test_only_level2_admins_switch_payment_mode(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    for uid, name in ((A1, "Аня"), (A3, "Вера"), (A4, "Глеб")):
        h.user(uid, name)
        await h.send(uid, "/start")
    for level, uid in (("1 уровень", A3), ("2 уровень", A4)):
        await h.send(A1, "/admin")
        await h.click(A1, "👮 Админы")
        await h.click(A1, f"➕ Пригласить: {level}" if level == "1 уровень" else f"➕ {level}")
        link = re.search(r"start=(adm_[\w-]+)", h.last_text(A1)).group(1)
        await h.send(uid, f"/start {link}")
    assert (config.admin_level(A3), config.admin_level(A4)) == (1, 2)

    await h.send(A3, "/admin")  # 1 уровень: ни кнопки, ни обхода подделанной кнопкой
    assert "⭐ Включить оплату звёздами" not in last_kb_labels(h, A3)
    for data in (AdminCb(s="s", a="pm"), AdminCb(s="s", a="pm_ask", v="stars"),
                 AdminCb(s="s", a="pm_go", v="stars")):
        await press_data(h, A3, data)
        assert h.alerts()[-1] == texts.ADMIN_L2_ONLY, data
    assert await mode(sessionmaker) == "test"

    await h.send(A4, "/admin")  # 2 уровень (не владелец) — может
    await h.click(A4, "⭐ Включить оплату звёздами")
    await h.click(A4, "✅ Да, включить звёзды")
    assert await mode(sessionmaker) == "stars"
    await press_data(h, A4, AdminCb(s="s", a="pm_go", v="stars"))  # двойное нажатие — второй раз ничего не делает
    assert "ℹ️ Уже включено: ⭐ Telegram Stars" in h.last_text(A4)
    async with sessionmaker() as db:
        assert len((await db.execute(select(AdminLog.id).where(AdminLog.action == "payments_mode"))).all()) == 1


async def test_manual_mode_needs_details_first(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    await h.send(A1, "/admin")
    await h.click(A1, "⚙️ Настройки")
    await h.click(A1, "💳 Оплата")
    await h.click(A1, "Режим оплаты: 🧪 тест")
    await h.click(A1, "🏦 Включить оплату переводом")
    assert h.last_text(A1).startswith("🏦 <b>Сначала укажи реквизиты")
    await press_data(h, A1, AdminCb(s="s", a="pm_go", v="manual"))  # и подделанной кнопкой — тоже нет
    assert await mode(sessionmaker) == "test"
    await h.click(A1, "✏️ Указать реквизиты")
    await h.send(A1, "СБП: +7 900 000-00-00, Т-Банк, Иван И.")
    await h.click(A1, "Режим оплаты: 🧪 тест")
    await h.click(A1, "🏦 Включить оплату переводом")
    assert h.last_text(A1).startswith("🏦 <b>Включить оплату переводом?</b>")
    await h.click(A1, "✅ Да, включить перевод")
    assert await mode(sessionmaker) == "manual"
    await h.click(A1, "⭐ Перейти на звёзды")
    assert "Уже выставленные счета старым способом остаются в силе" in h.last_text(A1)
    await h.click(A1, "✅ Да, включить звёзды")
    assert await mode(sessionmaker) == "stars"


async def test_start_in_real_mode_removes_test_leftovers(session, s) -> None:
    """Режим сменили в .env или в старой версии (тестовые деньги оставались) — уборка при запуске."""
    user = await make_user(session, A2)
    top = await paysvc.start_topup(session, user.id, 20000, s, now_utc())
    assert (await paysvc.confirm_payment(session, top.id, s, now_utc())).status == "topup_ok"
    pending = await paysvc.start_topup(session, user.id, 50000, s, now_utc())
    assert (await paymode.tidy_on_start(session, "test", now_utc())).money is False  # в тесте ничего не трогаем
    await set_setting(session, "payments_mode", "stars")
    await session.commit()
    removed = await paymode.tidy_on_start(session, "stars", now_utc())
    assert (removed.invoices, removed.rub, removed.bonus, removed.users) == (1, 20000, 0, 1)
    assert (await session.get(Payment, pending.id, populate_existing=True)).status == "cancelled"
    assert (await session.get(User, user.id, populate_existing=True)).balance_rub == 0
    assert not (await paymode.tidy_on_start(session, "stars", now_utc())).money  # повторно — нечего убирать
    assert await integrity_issues(session) == []
