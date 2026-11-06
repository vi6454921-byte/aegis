"""Новое в 2.4, пункт 8: раздел «🎨 Файлы и дизайн» (презентация, PDF, картинка, баннер, QR-код).

Фиксированная цена (презентация 50 ₽, остальное 20 ₽ за штуку), без срочности и без оценки админом.
Каждый тип доходит до оплаты и попадает во «📥 Входящие», шаблон Самарского университета сохраняет формат
и язык, готовый файл админа (через «✉️ Ответить») доходит до клиента, а «🔁 Нужны правки» уходит тому же админу."""
from __future__ import annotations

import itertools
import logging

from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import func, select

from app import texts
from app.bot.callbacks import AdminCb
from app.bot.keyboards import main_menu
from app.db.models import AdminLog, Order, OrderFile, Payment, User
from app.logging_setup import ACTIONS
from app.services import design as dsvc
from app.services import ledger, roles
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc
from tests.scenario import A1, A2, get

M = texts.MAIN_MENU
A3 = 3003  # второй админ (1 уровень)
_n = itertools.count(1)
SSAU = dsvc.STYLES["ssau"]


def kb_rows(h, uid: int) -> list[list[str]]:  # noqa: ANN001
    _, markup = h.screen(uid)[-1]
    return [[b.text for b in row] for row in (markup.inline_keyboard if markup else [])]


async def start(h, *uids: int) -> None:  # noqa: ANN001
    names = {A1: "Аня", A2: "Борис", A3: "Вера"}
    for uid in uids:
        h.user(uid, names.get(uid, "Гость"))
        await h.send(uid, "/start")


async def pick(h, uid: int, kind: str, *, style: str = "custom", fmt: str = "16:9", lang: str = "ru",  # noqa: ANN001
               qty: int = 1, entry: str = "menu") -> None:
    """Открыть раздел (из меню или с экрана «📝 Заказать»), выбрать тип, оформление и количество."""
    if entry == "menu":
        await h.send(uid, M["design"])
    else:
        await h.send(uid, M["order"])
        await h.click(uid, texts.DESIGN_FROM_ORDER_BTN)
    assert h.last_text(uid) == texts.DESIGN_SECTION
    await h.click(uid, dsvc.KINDS[kind], exact=True)
    if kind == "pres":
        await h.click(uid, dsvc.STYLES[style], exact=True)
        if style == "ssau":
            await h.click(uid, fmt, exact=True)
            await h.click(uid, dsvc.LANG_BUTTONS[lang], exact=True)
    assert "<b>Сколько штук?</b>" in h.last_text(uid)
    if qty in dsvc.QTY_BUTTONS:
        await h.click(uid, str(qty), exact=True)
    else:
        await h.click(uid, texts.DESIGN_QTY_OTHER, exact=True)
        await h.send(uid, str(qty))


async def send_task(h, uid: int, text: str = "Тема: история Самары, 10 слайдов") -> None:  # noqa: ANN001
    await h.send(uid, text)
    await h.send(uid, photo=f"dz-ph-{uid}-{next(_n)}")
    assert h.last_text(uid).startswith("✅ Получили:")
    await h.click(uid, texts.ANY_DONE_BTN, exact=True)


async def pay(h, uid: int) -> None:  # noqa: ANN001
    """«💳 Оплатить» в итоге → (условия) → тестовый счёт."""
    await h.click(uid, texts.DESIGN_PAY_BTN, exact=True)
    if any(texts.TERMS_ACCEPT_BTN in row for row in kb_rows(h, uid)):
        await h.click(uid, texts.TERMS_ACCEPT_BTN)
    await h.click(uid, texts.PAY_TEST_BTN)


async def last_design(sessionmaker, uid: int = A2) -> Order | None:  # noqa: ANN001
    async with sessionmaker() as db:
        return await db.scalar(select(Order).where(Order.user_id == uid, Order.subject == dsvc.DESIGN)
                               .order_by(Order.id.desc()))


async def inbox(h) -> str:  # noqa: ANN001
    await h.send(A1, "/admin")
    await h.click(A1, "📥 Входящие")
    return h.last_text(A1)


# ================================================================ кнопки и экраны
async def test_menu_button_and_section_grid(e2e) -> None:
    h, _ = e2e
    await start(h, A2)
    menu = main_menu(admin=False)
    assert isinstance(menu, ReplyKeyboardMarkup)
    rows = [[b.text for b in row] for row in menu.keyboard]
    assert [M["design"]] in rows and rows.index([M["design"]]) == 1  # сразу под «Заказать / Объяснить»

    grid = [[dsvc.KINDS["pres"], dsvc.KINDS["pdf"]], [dsvc.KINDS["img"], dsvc.KINDS["banner"]],
            [dsvc.KINDS["qr"], texts.BTN_BACK_ARROW]]
    assert grid == [["📊 Презентация", "📄 PDF-файл"], ["🖼 Картинка", "🪧 Баннер"], ["🔳 QR-код", "← Назад"]]
    await h.send(A2, M["design"])
    assert h.last_text(A2) == texts.DESIGN_SECTION
    assert "50 ₽" in texts.DESIGN_SECTION and "20 ₽" in texts.DESIGN_SECTION
    assert kb_rows(h, A2) == grid
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)
    assert h.last_text(A2) == texts.MENU_HINT

    # на экране «📝 Заказать»: кнопка раздела, «📎 Любая задача» по-прежнему над «Отмена»
    await h.send(A2, M["order"])
    rows = kb_rows(h, A2)
    assert [texts.DESIGN_FROM_ORDER_BTN] in rows
    assert rows[-2] == [texts.ANY_TASK_BTN] and texts.BTN_CANCEL in rows[-1]
    assert fmt_rub(5000) in h.last_text(A2) and fmt_rub(2000) in h.last_text(A2)  # цена на экране выбора предмета
    await h.click(A2, texts.DESIGN_FROM_ORDER_BTN)
    assert h.last_text(A2) == texts.DESIGN_SECTION and kb_rows(h, A2) == grid
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)  # обратно к выбору предмета
    assert [texts.DESIGN_FROM_ORDER_BTN] in kb_rows(h, A2)

    # презентация: оформление → шаблон → формат → язык; «← Назад» ведёт по шагам обратно
    await h.send(A2, M["design"])
    await h.click(A2, dsvc.KINDS["pres"], exact=True)
    assert kb_rows(h, A2) == [[dsvc.STYLES["custom"]], [SSAU], [texts.BTN_BACK_ARROW]]
    await h.click(A2, SSAU, exact=True)
    assert kb_rows(h, A2) == [["16:9", "4:3"], [texts.BTN_BACK_ARROW]]
    await h.click(A2, "4:3", exact=True)
    assert kb_rows(h, A2) == [["Русский", "English"], [texts.BTN_BACK_ARROW]]
    await h.click(A2, "English", exact=True)
    assert "🎓 Шаблон Самарского университета, 4:3, английский" in h.last_text(A2)
    assert kb_rows(h, A2) == [["1", "2", "3", "5", texts.DESIGN_QTY_OTHER], [texts.BTN_BACK_ARROW]]
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)
    assert kb_rows(h, A2) == [["Русский", "English"], [texts.BTN_BACK_ARROW]]
    h.assert_no_errors()


# ================================================================ каждый тип доходит до оплаты и «📥 Входящих»
async def test_each_kind_reaches_payment_and_inbox(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, A2)
    ids = {}
    for i, kind in enumerate(dsvc.KINDS):
        await pick(h, A2, kind, entry="order" if kind == "qr" else "menu")  # QR-код - с экрана «📝 Заказать»
        await send_task(h, A2)
        price = dsvc.PRICES[kind]
        summary = h.last_text(A2)
        assert f"{dsvc.KINDS[kind]} × 1: <b>{fmt_rub(price)}</b>" in summary
        assert kb_rows(h, A2) == [[texts.DESIGN_PAY_BTN], [texts.DESIGN_EDIT_BTN, texts.DESIGN_CANCEL_BTN]]
        await h.click(A2, texts.DESIGN_PAY_BTN, exact=True)
        if i == 0:  # первый заказ: сначала условия
            await h.click(A2, texts.TERMS_ACCEPT_BTN)
        order = await last_design(sessionmaker)
        assert (order.status, order.design_kind, order.task_count, order.price_final) == \
            ("awaiting_payment", kind, 1, price)
        assert texts.DESIGN_CREATED.format(id=order.id, what=dsvc.describe(order), price=fmt_rub(price)) \
            in h.texts(A2)
        if i == 0:  # до оплаты во «Входящих» его нет, админам не писали
            assert f"#{order.id}" not in await inbox(h)
            assert not any(f"Новый заказ #{order.id}" in t for t in h.texts(A1))
        await h.click(A2, texts.PAY_TEST_BTN)
        order = await get(sessionmaker, Order, order.id)
        assert (order.status, order.paid_invoice, order.is_test) == ("paid", price, True)
        assert texts.DESIGN_PAID.format(id=order.id, what=dsvc.describe(order)) in h.texts(A2)
        alert = next(t for t in h.texts(A1) if t.startswith(f"🎨 <b>Новый заказ #{order.id}: файлы и дизайн</b>"))
        assert f"Что: {dsvc.what(order)}" in alert and "история Самары" in alert
        async with sessionmaker() as db:
            pay_row = await db.scalar(select(Payment).where(Payment.order_id == order.id))
            files = await db.scalar(select(func.count(OrderFile.id)).where(OrderFile.order_id == order.id))
        assert (pay_row.status, pay_row.amount_rub, files) == ("paid", price, 1)
        ids[kind] = order.id

    box = await inbox(h)
    for kind, oid in ids.items():
        assert f"#{oid}" in box, kind
    assert "🎨" in box
    await h.send(A2, M["my_orders"])
    listing = h.last_text(A2)
    for kind, oid in ids.items():
        h.find_button(A2, f"#{oid} · {texts.STATUS['paid']}")
        assert dsvc.KINDS[kind] in listing
    h.assert_no_errors()


# ================================================================ шаблон Самарского университета
async def test_ssau_template_keeps_format_and_language(e2e, sessionmaker, caplog) -> None:
    caplog.set_level(logging.INFO, logger=ACTIONS)
    h, _ = e2e
    await start(h, A1, A2)
    await pick(h, A2, "pres", style="ssau", fmt="16:9", lang="ru", qty=2, entry="order")
    await send_task(h, A2, "Доклад по сопромату")
    summary = h.last_text(A2)
    assert "Оформление: 🎓 Шаблон Самарского университета, 16:9, русский" in summary
    assert f"📊 Презентация × 2: <b>{fmt_rub(10000)}</b>" in summary
    await pay(h, A2)
    order = await last_design(sessionmaker)
    assert (order.status, order.design_kind, order.design_style, order.design_format, order.design_lang) == \
        ("paid", "pres", "ssau", "16:9", "ru")
    assert (order.task_count, order.price_final) == (2, 10000)
    alert = next(t for t in h.texts(A1) if t.startswith(f"🎨 <b>Новый заказ #{order.id}"))
    assert "Оформление: 🎓 Шаблон Самарского университета, 16:9, русский" in alert
    assert dsvc.SSAU_URL in alert

    # второй: 4:3 и английский
    await pick(h, A2, "pres", style="ssau", fmt="4:3", lang="en", qty=1)
    await send_task(h, A2, "Slides about Samara")
    await pay(h, A2)
    second = await last_design(sessionmaker)
    assert (second.design_format, second.design_lang, second.price_final) == ("4:3", "en", 5000)
    alert = next(t for t in h.texts(A1) if t.startswith(f"🎨 <b>Новый заказ #{second.id}"))
    assert "🎓 Шаблон Самарского университета, 4:3, английский" in alert

    # видно в «📦 Мои заказы», в карточке клиента и админа, в журнале
    await h.send(A2, M["my_orders"])
    assert "16:9, русский" in h.last_text(A2) and "4:3, английский" in h.last_text(A2)
    await h.click(A2, f"#{order.id} · ")
    assert "🎓 Шаблон Самарского университета, 16:9, русский" in h.last_text(A2)
    await h.click_data(A1, AdminCb(s="o", a="open", id=order.id).pack())
    card = h.last_text(A1)
    assert "🎓 Шаблон Самарского университета, 16:9, русский" in card and dsvc.SSAU_URL in card
    logged = [r.getMessage() for r in caplog.records if r.name == ACTIONS]
    assert any(f"создал заказ #{order.id}" in m and "16:9, русский" in m for m in logged)
    assert any(f"оплатил заказ #{second.id}" in m and "4:3, английский" in m for m in logged)


async def test_custom_design_has_no_format_and_language(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, A2)
    await pick(h, A2, "pres", style="ssau", fmt="16:9", lang="en")  # передумал: назад к оформлению
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)  # к количеству
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)  # к языку
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)  # к формату
    await h.click(A2, texts.BTN_BACK_ARROW, exact=True)  # к оформлению
    await h.click(A2, dsvc.STYLES["custom"], exact=True)
    await h.click(A2, "1", exact=True)
    await send_task(h, A2)
    assert "Оформление: 🎨 Свой дизайн" in h.last_text(A2)
    await pay(h, A2)
    order = await last_design(sessionmaker)
    assert (order.design_style, order.design_format, order.design_lang) == ("custom", None, None)


# ================================================================ цены: 50 × N и 20 × N
def test_prices_are_fixed_per_piece() -> None:
    for n in range(1, dsvc.MAX_QTY + 1):
        assert dsvc.total("pres", n) == 5000 * n
        for kind in ("pdf", "img", "banner", "qr"):
            assert dsvc.total(kind, n) == 2000 * n
    assert dsvc.price_line("img", 3) == f"🖼 Картинка × 3: <b>{fmt_rub(6000)}</b>"
    assert dsvc.price_line("pres", 2) == f"📊 Презентация × 2: <b>{fmt_rub(10000)}</b>"


async def test_quantity_buttons_other_and_edit(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, A2)
    await pick(h, A2, "img", qty=3)
    await send_task(h, A2, "Логотип для кружка")
    assert f"🖼 Картинка × 3: <b>{fmt_rub(6000)}</b>" in h.last_text(A2)

    # «✏️ Изменить»: выбор заново, задание сохраняется; «Другое»: от 1 до 50
    await h.click(A2, texts.DESIGN_EDIT_BTN, exact=True)
    assert h.last_text(A2).startswith(texts.DESIGN_EDIT_NOTE)
    await h.click(A2, dsvc.KINDS["banner"], exact=True)
    await h.click(A2, texts.DESIGN_QTY_OTHER, exact=True)
    assert h.last_text(A2) == texts.DESIGN_QTY_PROMPT.format(max=50)
    for bad in ("0", "51", "много", "-3"):
        await h.send(A2, bad)
        assert h.last_text(A2) == texts.DESIGN_QTY_BAD.format(max=50), bad
    await h.send(A2, "7")
    assert texts.DESIGN_SENT.format(what="файлов: 1, " + texts.DESIGN_WHAT_TEXT) in h.last_text(A2)
    await h.click(A2, texts.ANY_DONE_BTN, exact=True)
    summary = h.last_text(A2)
    assert f"🪧 Баннер × 7: <b>{fmt_rub(14000)}</b>" in summary and "Логотип для кружка" in summary
    await pay(h, A2)
    order = await last_design(sessionmaker)
    assert (order.status, order.design_kind, order.task_count, order.price_final) == ("paid", "banner", 7, 14000)
    assert order.deadline_at is None  # без срочности
    assert "Логотип для кружка" in (order.comment or "")
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(Order.id)).where(Order.subject == dsvc.DESIGN)) == 1


async def test_empty_task_and_cancel(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A2)
    await pick(h, A2, "pdf")
    await h.click(A2, texts.ANY_DONE_BTN, exact=True)
    assert texts.DESIGN_NEED_CONTENT in h.alerts()
    await h.click(A2, texts.DESIGN_CANCEL_BTN, exact=True)
    assert h.last_text(A2) == texts.DESIGN_CANCELLED

    await pick(h, A2, "qr", qty=2)
    await send_task(h, A2, "QR на ссылку https://example.com")
    await h.click(A2, texts.DESIGN_CANCEL_BTN, exact=True)
    assert h.last_text(A2) == texts.DESIGN_CANCELLED
    assert await last_design(sessionmaker) is None  # заказ создаётся только на «💳 Оплатить»


# ================================================================ оплата: бонусы, баланс, безлимит
async def test_bonuses_balance_and_invoice(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, A2)
    async with sessionmaker() as db:
        user = await db.get(User, A2)
        await ledger.credit_bonus(db, user, 30000, source="admin", kind="admin_credit", ttl_days=30, now=now_utc())
        await ledger.credit_rub(db, user, 1000, "admin_credit")
        await db.commit()
    await pick(h, A2, "pres", qty=5)
    await send_task(h, A2)
    assert f"📊 Презентация × 5: <b>{fmt_rub(25000)}</b>" in h.last_text(A2)
    assert "💰 Оплата: сначала бонусы" in h.last_text(A2)
    await pay(h, A2)
    order = await last_design(sessionmaker)
    # бонусами не больше 50% цены, потом баланс, остаток счётом
    assert (order.status, order.paid_bonus, order.paid_balance, order.paid_invoice) == ("paid", 12500, 1000, 11500)

    async with sessionmaker() as db:  # хватает баланса: без счёта
        user = await db.get(User, A2)
        await ledger.credit_rub(db, user, 10000, "admin_credit")
        await db.commit()
    await pick(h, A2, "qr", qty=2)
    await send_task(h, A2)
    await h.click(A2, texts.DESIGN_PAY_BTN, exact=True)
    await h.click(A2, texts.PAY_BALANCE_BTN)
    order = await last_design(sessionmaker)
    assert (order.status, order.price_final, order.paid_invoice) == ("paid", 4000, 0)
    assert order.paid_bonus + order.paid_balance == 4000
    assert any(t.startswith(f"🎨 <b>Новый заказ #{order.id}") for t in h.texts(A1))


async def test_unlimited_user_orders_for_free(e2e, sessionmaker) -> None:
    h, _ = e2e
    await start(h, A1, A2)
    async with sessionmaker() as db:
        user = await db.get(User, A2)
        user.unlimited_forever = True
        user.unlimited_since = now_utc()
        await db.commit()
    await pick(h, A2, "pres", style="ssau", fmt="4:3", lang="ru", qty=3)
    await send_task(h, A2)
    assert texts.DESIGN_UNLIMITED_LINE in h.last_text(A2)
    await h.click(A2, texts.DESIGN_SEND_UNLIMITED_BTN, exact=True)
    await h.click(A2, texts.TERMS_ACCEPT_BTN)
    order = await last_design(sessionmaker)
    assert (order.status, order.unlimited, order.price_final) == ("paid", True, 0)
    assert texts.DESIGN_UNLIMITED.format(id=order.id, what=dsvc.describe(order)) in h.texts(A2)
    alert = next(t for t in h.texts(A1) if t.startswith(f"🎨 <b>Новый заказ #{order.id}"))
    assert texts.ADMIN_DESIGN_UNLIMITED in alert and "4:3, русский" in alert
    assert f"#{order.id}" in await inbox(h)


# ================================================================ результат, правки тому же админу, «✅ Принять»
async def test_admin_result_reaches_user_redo_goes_to_same_admin(e2e, sessionmaker, config, caplog) -> None:
    caplog.set_level(logging.INFO, logger=ACTIONS)
    h, _ = e2e
    await start(h, A1, A2, A3)
    async with sessionmaker() as db:
        await roles.set_level(db, config, A3, 1, A1, now_utc())
    await pick(h, A2, "img", qty=2)
    await send_task(h, A2, "Две картинки для поста")
    await pay(h, A2)
    order = await last_design(sessionmaker)
    oid = order.id
    for admin in (A1, A3):  # оба админа видят новый оплаченный заказ
        assert any(t.startswith(f"🎨 <b>Новый заказ #{oid}") for t in h.texts(admin)), admin

    # админ 1: «✉️ Ответить» → пишет → присылает файл
    await h.click_data(A1, AdminCb(s="c", a="o", id=oid).pack())
    await h.send(A1, "Взяли, через час пришлём")
    order = await get(sessionmaker, Order, oid)
    assert (order.status, order.assignee_id) == ("in_work", A1)
    assert h.last_text(A2).endswith("Взяли, через час пришлём")
    copies = len(h.calls("CopyMessage"))
    await h.send(A1, document=("dz-res-1", "result.png", b"\x89PNG result"))
    assert len(h.calls("CopyMessage")) == copies + 1
    copy = h.calls("CopyMessage")[-1]
    assert (str(copy.chat_id), str(copy.from_chat_id)) == (str(A2), str(A1))
    order = await get(sessionmaker, Order, oid)
    assert order.status == "done"
    assert texts.ADMIN_DESIGN_DONE_NOTE.format(id=oid) in h.last_text(A1)
    assert h.last_text(A2) == texts.DESIGN_RESULT.format(id=oid, what=dsvc.describe(order))
    assert kb_rows(h, A2) == [[texts.DESIGN_ACCEPT_BTN, texts.DESIGN_REDO_BTN]]
    await h.send(A2, M["my_orders"])
    h.find_button(A2, f"#{oid} · {texts.DESIGN_STATUS['done']}")

    # клиент: «🔁 Нужны правки» → правки только админу 1 (тот, кто делал)
    a3_before = len(h.texts(A3))
    await h.click(A2, texts.DESIGN_REDO_BTN)
    assert h.last_text(A2) == texts.DESIGN_REDO_PROMPT.format(id=oid)
    await h.send(A2, "Сделайте фон светлее")
    order = await get(sessionmaker, Order, oid)
    assert (order.status, order.redo_count, order.assignee_id) == ("in_work", 1, A1)
    assert any("Сделайте фон светлее" in t for t in h.texts(A1))
    assert any(texts.DESIGN_REDO_NOTE.format(id=oid, what=dsvc.describe(order)) in t for t in h.texts(A1))
    assert not any("Сделайте фон светлее" in t for t in h.texts(A3)[a3_before:])
    assert not any(f"Правки по заказу #{oid}" in t for t in h.texts(A3))
    assert h.last_text(A2) == texts.DESIGN_REDO_ACK
    await h.click(A2, texts.DESIGN_REDO_DONE_BTN)
    assert h.last_text(A2) == texts.DESIGN_REDO_WAIT

    # исправленный файл → снова готово → «✅ Принять»
    await h.send(A1, document=("dz-res-2", "result-v2.png", b"\x89PNG result v2"))
    order = await get(sessionmaker, Order, oid)
    assert order.status == "done"
    assert h.last_text(A2) == texts.DESIGN_RESULT.format(id=oid, what=dsvc.describe(order))
    await h.click(A2, texts.DESIGN_ACCEPT_BTN)
    assert h.last_text(A2) == texts.DESIGN_ACCEPTED.format(id=oid)
    order = await get(sessionmaker, Order, oid)
    assert order.status == "accepted" and order.closed_at is not None
    assert any(t.startswith(f"✅ Заказ #{oid} ({dsvc.describe(order)}) принят клиентом") for t in h.texts(A1))
    assert not any(t.startswith(f"✅ Заказ #{oid} (") for t in h.texts(A3))
    await h.send(A2, M["my_orders"])
    h.find_button(A2, f"#{oid} · {texts.DESIGN_STATUS['accepted']}")

    async with sessionmaker() as db:
        delivered = await db.scalar(select(func.count(AdminLog.id)).where(AdminLog.action == "design_delivered",
                                                                           AdminLog.target_id == str(oid)))
    assert delivered == 2
    logged = [r.getMessage() for r in caplog.records if r.name == ACTIONS]
    assert any(f"отправил результат по заказу #{oid}" in m for m in logged)
    assert any(f"попросил правки по заказу #{oid}" in m for m in logged)
    assert any(f"принял заказ #{oid}" in m for m in logged)
    h.assert_no_errors()


def test_texts_do_not_claim_university_affiliation() -> None:
    blob = " ".join(v for k, v in vars(texts).items() if k.startswith(("DESIGN", "ADMIN_DESIGN")) and isinstance(v, str))
    low = blob.lower()
    for phrase in ("официальный бот", "бот университета", "партнёр университета", "от университета"):
        assert phrase not in low
    assert "—" not in blob and "–" not in blob


def test_university_is_samara_university_not_samgu() -> None:
    """Шаблон: Самарский университет им. Королёва (ssau.ru). СамГУ: другой вуз, его названия нигде нет."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    wrong = re.compile(r"самгу|samgu|samsu|самарск\w*\s+государственн", re.I)
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".txt", ".example"}
             and not {"data", "__pycache__", ".git"} & set(p.parts) and p.name != Path(__file__).name]
    found = [f"{p.relative_to(root)}: {m.group(0)}" for p in files
             for m in wrong.finditer(p.read_text(encoding="utf-8", errors="ignore"))]
    assert not found, found
    assert dsvc.STYLES["ssau"] == "🎓 Шаблон Самарского университета"
    assert dsvc.SSAU_URL == "https://ssau.ru/info/style/doc"
