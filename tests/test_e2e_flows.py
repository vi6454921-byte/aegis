"""Другие пути через бота: «Зачли», переделка после спора, отмена, отказ, режим без Mini App, ссылки на подарки
из админки, оплата и возврат Telegram Stars и «прокликивание» всех кнопок клиента и админа."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin import broadcast
from app.db.models import Order, Payment, Referral, User
from app.services.maintenance import integrity_issues
from app.utils.money import fmt_rub, stars_for
from tests.scenario import (
    A1,
    A2,
    GIFT_URLS,
    admin_deliver,
    admin_invoice,
    get,
    gift_hrefs,
    path,
    run_scenario,
    set_settings,
    start_with_referral,
    upload_solution,
    user_pays,
    wizard_order,
)

V2 = "<!doctype html><html><head><meta charset='utf-8'></head><body><h1>Версия 2</h1></body></html>".encode()


async def test_accept_redo_and_single_referral_reward(e2e, config, sessionmaker) -> None:
    h, web = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await set_settings(sessionmaker, referral_bonus=5000)
    await start_with_referral(h, A1, A2)

    # первый заказ без конспектов: 200 ₽ → решение → «Зачли»
    oid = await wizard_order(h, A2, notes=False)
    assert (await get(sessionmaker, Order, oid)).lecture_status == "none"
    await admin_invoice(h, oid, 20000)
    await user_pays(h, A2, 20000)
    await admin_deliver(h, oid)
    await h.click(A2, texts.ACCEPT_BTN)
    assert texts.ACCEPTED in h.texts(A2)
    assert f"🎓 Заказ #{oid} зачли" in h.last_text(A1)
    assert (await get(sessionmaker, Order, oid)).status == "accepted"
    assert (await get(sessionmaker, User, A1)).balance_bonus == 5000

    # второй заказ: «Не зачли» → «Переделать» → новая версия → «Зачли»
    oid2 = await wizard_order(h, A2, notes=False)
    assert oid2 != oid and texts.TERMS_ACCEPT_BTN not in h.texts(A2)[-3:]  # условия уже приняты
    await admin_invoice(h, oid2, 20000)
    await user_pays(h, A2, 20000)
    await admin_deliver(h, oid2)
    _, sol_btn = h.find_button(A2, texts.OPEN_SOLUTION)
    await h.click(A2, texts.DISPUTE_BTN)
    await h.send(A2, photo="remark-2")
    await h.click(A1, "🔁 Переделать")
    assert texts.REDO.format(id=oid2) in h.texts(A2)
    order = await get(sessionmaker, Order, oid2)
    assert order.status == "in_work"
    assert "Эпюра Монжа" in await (await web.get(path(sol_btn.web_app.url))).text()  # старая версия видна
    await h.click(A1, "📤 Заменить решение")
    await upload_solution(h, V2, "v2.html")
    await h.click(A1, "📦 Готово, отправить клиенту")
    assert h.texts(A2).count(texts.ORDER_DONE.format(id=oid2)) == 2
    page = await (await web.get(path(sol_btn.web_app.url))).text()
    assert "Версия 2" in page and "Эпюра" not in page  # ссылка та же, решение новое
    await h.click(A2, texts.ACCEPT_BTN)
    assert (await get(sessionmaker, Order, oid2)).status == "accepted"
    assert (await get(sessionmaker, User, A1)).balance_bonus == 5000  # бонус за друга — только один раз
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(Referral.id)).where(Referral.rewarded.is_(True))) == 1
        assert await integrity_issues(db) == []


async def test_user_cancel_and_admin_reject(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    oid = await wizard_order(h, A2, notes=True)
    await h.send(A2, texts.MAIN_MENU["my_orders"])
    await h.click(A2, f"#{oid} ·")
    await h.click(A2, "✖️ Отменить заказ")
    await h.click(A2, "Да, отменить")
    assert (await get(sessionmaker, Order, oid)).status == "cancelled"
    assert f"отменил заказ #{oid}" in h.last_text(A1)

    oid2 = await wizard_order(h, A2, notes=False, days=6)
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid2).pack())
    await h.click(A1, "🚫 Отклонить")
    await h.send(A1, "Это не начерталка")
    assert (await get(sessionmaker, Order, oid2)).status == "rejected"
    assert texts.ORDER_REJECTED.format(id=oid2, reason="\n\nПричина: Это не начерталка") in h.texts(A2)
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []


async def test_without_mini_app_solutions_are_sent_as_files(e2e, config, sessionmaker) -> None:
    config.webapp_base_url = ""  # нет домена — решения приходят файлами, подарки — те же ссылки в тексте
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    await h.send(A2, texts.MAIN_MENU["gifts"])
    assert gift_hrefs(h.last_text(A2)) == list(GIFT_URLS) and not h.sent_documents(A2)

    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    await user_pays(h, A2, 20000)
    await admin_deliver(h, oid)
    sent = [d for d in h.sent_documents(A2) if (d.caption or "").startswith(f"🧩 Решение по заказу #{oid}")]
    assert len(sent) == 1
    await h.click(A2, texts.OPEN_SOLUTION)  # без Mini App кнопка присылает файл ещё раз
    sent = [d for d in h.sent_documents(A2) if (d.caption or "").startswith(f"🧩 Решение по заказу #{oid}")]
    assert len(sent) == 2


async def test_admin_edits_gift_links(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A2, "/start")
    await h.send(A1, "/admin")
    await h.click(A1, "⚙️ Настройки")
    await h.click(A1, "🎁 Подарки (ссылки)")
    screen = h.last_text(A1)
    assert "Так видят клиенты" in screen and gift_hrefs(screen) == list(GIFT_URLS)

    await h.click(A1, "🧮 Расчётка:")
    await h.send(A1, "просто текст")
    assert h.last_text(A1).startswith("⚠️ Нужна ссылка вида https://")
    await h.send(A1, "example.github.io/rgr-15/")  # без https:// — допишем сами
    assert h.last_text(A1).startswith("✅ 🧮 Расчётка: example.github.io/rgr-15/")
    await h.click(A1, "🔢 Матрицы:")
    await h.send(A1, "-")  # скрыть подарок
    assert "🔢 Матрицы: <b>скрыт</b>" in h.last_text(A1)

    await h.send(A2, "/start")
    welcome = next(m.text for m, _ in reversed(h.screen(A2)) if "Привет! Это Аегис" in (m.text or ""))
    assert gift_hrefs(welcome) == ["https://example.github.io/rgr-15/", GIFT_URLS[1]]
    await h.send(A2, texts.MAIN_MENU["gifts"])
    assert gift_hrefs(h.last_text(A2)) == ["https://example.github.io/rgr-15/", GIFT_URLS[1]]

    await h.click(A1, "🔢 Матрицы:")
    await h.click(A1, "↩️ По умолчанию")
    await h.send(A2, texts.MAIN_MENU["gifts"])
    assert gift_hrefs(h.last_text(A2)) == ["https://example.github.io/rgr-15/", *GIFT_URLS[1:]]

    for key in ("gift_url_calc", "gift_url_nach", "gift_url_matrices"):  # все скрыты — подарков нет
        await set_settings(sessionmaker, **{key: ""})
    await h.send(A2, texts.MAIN_MENU["gifts"])
    assert h.last_text(A2).startswith(texts.GIFTS_EMPTY)
    await h.send(A2, "/start")
    welcome = next(m.text for m, _ in reversed(h.screen(A2)) if "Привет! Это Аегис" in (m.text or ""))
    assert "{gifts}" not in welcome and texts.GIFTS_WELCOME_TITLE not in welcome and welcome.endswith("деньги.")


async def test_click_every_button(e2e, config, sessionmaker) -> None:
    """После полного сценария жмём все кнопки, что видит клиент, затем все кнопки админки: ни одной ошибки."""
    h, web = e2e
    await run_scenario(h, web, config, sessionmaker)
    for text in (*texts.MAIN_MENU.values(), "/help", "/terms", "/paysupport", "/support", "/cancel", "/admin"):
        await h.send(A2, text)
    assert texts.ADMIN_ONLY in h.texts(A2)
    pressed = await h.crawl(A2, entry=texts.MAIN_MENU["balance"])
    assert len(pressed) > 20, pressed
    for label in (texts.GUIDE_BTN, texts.GUIDE_NEXT, texts.GUIDE_DONE, "⏰ Сроки и цены", "📱 Оставить номер",
                  "💬 Поддержка", texts.CHANNEL_CHECK_BTN, "Завтра", "Через 2 дня"):  # v2.4: у начерталки срок на цену не влияет
        assert label in pressed, label

    pressed = await h.crawl(A1, entry="/admin", skip=("AEGIS-",))  # десятки кодов в 🎟 Промокодах — по одному не жмём
    assert len(pressed) > 60, pressed
    for label in ("📊 Excel-таблица", "👮 Админы", "➕ Пригласить: 1 уровень", "⏱ Срочность",
                  "📢 Канал и бонусы за подписку", "🔔 Уведомления", "❓ Справка"):
        assert label in pressed, label
    for label in ("📊 Статистика", "💾 Бэкап сейчас", "🩺 Проверка целостности", "🧹 Да, удалить",
                  "🚫 Забанить", "↩️ На баланс", "🎁 Подарки (ссылки)", "🧮 Расчётка:",
                  "⭐ Включить оплату звёздами", "✅ Да, включить", "🧪 Вернуть тестовую оплату", "✅ Да, вернуть тест"):
        assert any(label in p for p in pressed), label
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []
        assert await db.scalar(select(func.count(Order.id)).where(Order.is_test.is_(True))) == 0  # тестовое удалено


async def test_telegram_stars_payment_and_star_refund(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    await h.send(A1, "/admin")  # переключаем режим оплаты кнопкой, как в инструкции
    await h.click(A1, "⚙️ Настройки")
    await h.click(A1, "💳 Оплата")
    await h.click(A1, "Режим оплаты: 🧪 тест")
    await h.click(A1, "⭐ Включить оплату звёздами")
    await h.click(A1, "✅ Да, включить звёзды")
    assert "Сейчас: <b>⭐ Telegram Stars</b>" in h.last_text(A1)

    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    stars = stars_for(20000, "1.75")
    assert f"Счётом: <b>{stars} ⭐</b>" in h.last_text(A2)
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")
    [inv] = h.calls("SendInvoice")
    assert inv.currency == "XTR" and inv.provider_token == "" and inv.prices[0].amount == stars

    assert not await h.pre_checkout(A2, inv.payload, stars - 1)  # сумма не та — отказ
    assert not await h.pre_checkout(A1, inv.payload, stars)  # чужой счёт — отказ
    assert await h.pre_checkout(A2, inv.payload, stars)
    await h.successful_payment(A2, inv.payload, stars, "stars-charge-1")
    assert texts.PAY_DONE_ORDER.format(id=oid) in h.texts(A2)
    await h.successful_payment(A2, inv.payload, stars, "stars-charge-1")  # Telegram прислал повтор
    assert h.last_text(A2) == "Этот платёж уже учтён ✅"
    order = await get(sessionmaker, Order, oid)
    assert order.status == "paid" and not order.is_test and order.paid_invoice == 20000

    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    await h.click(A1, "↩️ Вернуть деньги")
    await h.click(A1, f"⭐ Звёздами ({stars})")
    [refund] = h.calls("RefundStarPayment")
    assert refund.telegram_payment_charge_id == "stars-charge-1" and refund.user_id == A2
    assert texts.REFUNDED.format(id=oid, details=f"{stars} ⭐ звёздами") in h.texts(A2)
    async with sessionmaker() as db:
        pay = await db.scalar(select(Payment).where(Payment.order_id == oid))
        assert (pay.status, pay.refund_method) == ("refunded", "stars")
        assert (await db.get(User, A2)).balance_rub == 0  # вернули звёздами, а не на баланс
        assert await integrity_issues(db) == []


async def test_broadcast_with_preview_and_blocked_user(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    a3 = 3003
    for uid, name in ((A1, "Аня"), (A2, "Борис"), (a3, "Вера")):
        h.user(uid, name)
        await h.send(uid, "/start")
    await h.block_bot(a3)  # Вера заблокировала бота — пришёл my_chat_member
    assert (await get(sessionmaker, User, a3)).bot_blocked
    await h.block_bot(a3, blocked=False)  # …и разблокировала
    assert not (await get(sessionmaker, User, a3)).bot_blocked
    h.fake.blocked.add(a3)  # снова заблокировала, но бот об этом ещё не знает

    await h.send(A1, "/admin")
    await h.click(A1, "📣 Рассылка")
    await h.click(A1, "Все: 3")
    await h.send(A1, "Скидки на выходных! 🎉")
    assert h.texts(A1)[-2] == "👀 <b>Предпросмотр</b>: так увидят получатели:"
    assert h.last_text(A1).startswith("Отправить 3 получателям")
    send_msg, send_btn = h.find_button(A1, "🚀 Отправить")
    await h.press(A1, send_msg, send_btn)
    await asyncio.gather(*list(broadcast._tasks))
    assert h.last_text(A1).startswith("📣 Рассылка #1 завершена.\nДоставлено: <b>2</b> из 3 · заблокировали бота: 1")
    copies = [m for m in h.calls("CopyMessage") if m.chat_id in (A1, A2)]
    assert len(copies) == 3  # предпросмотр админу + две доставки
    assert (await get(sessionmaker, User, a3)).bot_blocked
    await h.send(A1, "/admin")
    await h.click(A1, "📊 Статистика")
    assert "заблокировали бота 1" in h.last_text(A1)
    await h.click(A1, "🛡 В админку")
    await h.click(A1, "📣 Рассылка")
    h.find_button(A1, "Все: 2")  # заблокировавших в рассылку не берём
    await h.press(A1, send_msg, send_btn)  # двойное нажатие до того, как кнопка исчезла, — без дубля
    assert "уже отправлена" in h.alerts()[-1]
    assert len([m for m in h.calls("CopyMessage") if m.chat_id == A2]) == 1
