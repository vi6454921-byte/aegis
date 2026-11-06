"""Новое во второй версии: обжалование бана, уровни админов и приглашения, переписка свайпом,
объяснение материала, обучалка, бонусы за канал, оплата переводом, Excel, напоминания, наценка за срочность."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone

import openpyxl
import pytest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton
from sqlalchemy import func, select

from app import scheduler, texts
from app.bot.callbacks import AdminCb, MenuCb
from app.bot.handlers import banned
from app.db.models import Admin, Order, Payment, Referral, Ticket, TicketMessage, User
from app.services import channel as chsvc
from app.services import roles
from app.services.maintenance import integrity_issues
from app.services.settings import load_settings
from app.utils.money import fmt_rub
from app.utils.timeutil import TZ, WEEKDAYS, local_today, now_utc
from tests.fakebot import make_bot
from tests.scenario import (
    A1,
    A2,
    admin_invoice,
    get,
    set_settings,
    start_with_referral,
    user_pays,
    wizard_order,
)

A3, A4, A5 = 3003, 4004, 5005
GUIDE_HEAD = "📖 <b>Как пользоваться ботом</b>"
R50 = fmt_rub(5000)


@pytest.fixture(autouse=True)
def _fresh_globals() -> None:
    """Состояние процесса, которое иначе протекло бы между тестами (здоровье канала сбрасывает conftest)."""
    banned._nagged.clear()  # noqa: SLF001


def labels(h, uid: int) -> list[str]:  # noqa: ANN001
    """Тексты всех кнопок, которые сейчас на экране uid."""
    return [b.text for _, kb in h.screen(uid) if kb for row in kb.inline_keyboard for b in row]


def last_kb_labels(h, uid: int) -> list[str]:  # noqa: ANN001
    _, kb = h.screen(uid)[-1]
    return [b.text for row in (kb.inline_keyboard if kb else []) for b in row]


def message_with(h, uid: int, prefix: str):  # noqa: ANN001, ANN201
    """Актуальная версия последнего сообщения uid, текст которого начинается с prefix: (сообщение, кнопки)."""
    for msg, kb in reversed(h.screen(uid)):
        if (msg.text or msg.caption or "").startswith(prefix):
            return msg, [b.text for row in (kb.inline_keyboard if kb else []) for b in row]
    raise AssertionError(f"нет сообщения «{prefix}…» у {uid}: {h.texts(uid)[-3:]}")


async def press_data(h, uid: int, data: CallbackData) -> None:  # noqa: ANN001
    """Нажать кнопку, которой у uid на экране нет (старая кнопка / подделанный callback)."""
    await h.press(uid, h.last_message(uid), InlineKeyboardButton(text="•", callback_data=data.pack()))


# ---------------------------------------------------------------- 1. бан и обжалование
async def test_banned_user_appeals_and_admin_unbans(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")

    await h.send(A1, "/admin")
    await h.click(A1, "👥 Пользователи")
    await h.click(A1, "Борис")
    await h.click(A1, "🚫 Забанить")
    await h.click(A1, "Спам", exact=True)
    user = await get(sessionmaker, User, A2)
    assert user.is_banned and user.ban_reason == "Спам"
    assert texts.USER_BANNED_NOTICE.format(reason=": Спам") in h.texts(A2)
    h.find_button(A2, texts.APPEAL_BTN)

    # меню закрыто, но кнопка обжалования есть всегда
    await h.send(A2, texts.MAIN_MENU["order"])
    assert texts.BANNED_KB_REMOVED in h.texts(A2)
    assert h.last_text(A2) == texts.BANNED.format(reason=texts.BANNED_REASON.format(reason="Спам"))
    await h.send(A2, "/start")
    assert h.last_text(A2).startswith("🚫 <b>Доступ к боту ограничен</b>")

    await h.click(A2, texts.APPEAL_BTN)
    assert h.last_text(A2) == texts.APPEAL_PROMPT
    await h.send(A2, "Это ошибка, я ничего не рассылал")
    assert h.last_text(A2) == texts.APPEAL_SENT
    header, buttons = message_with(h, A1, "📨 Обжалование бана · №")
    assert "🚫 Забанен: Спам" in header.text and "Это ошибка" in header.text
    assert "✅ Разбанить" in buttons and "✉️ Ответить" in buttons

    await h.click(A2, texts.APPEAL_BTN)  # второе обращение сразу — «уже на рассмотрении»
    assert h.last_text(A2).startswith("⏳ Обращение уже на рассмотрении")

    # админ отвечает свайпом — бан ещё действует, но переписка идёт
    await h.send(A1, "Проверим и вернёмся с ответом", reply_to=header)
    assert h.last_text(A2) == f"{texts.SUPPORT_FROM_TEAM}\n\nПроверим и вернёмся с ответом"
    assert h.last_text(A1).startswith(f"{texts.CHAT_DELIVERED} · По обращению №")
    await h.send(A2, "Спасибо, жду", reply_to=h.last_message(A2))  # забаненный отвечает свайпом
    assert h.last_text(A2) == texts.APPEAL_SENT
    await h.click(A2, texts.SUPPORT_REPLY_BTN)  # …или кнопкой «💬 Ответить» — без ожидания 12 часов
    assert h.last_text(A2) == texts.APPEAL_PROMPT
    await h.send(A2, photo="appeal-proof")
    assert h.last_text(A2) == texts.APPEAL_SENT

    await h.click(A1, "✅ Разбанить")
    assert texts.USER_UNBANNED in h.texts(A2)
    assert not (await get(sessionmaker, User, A2)).is_banned
    async with sessionmaker() as db:
        [ticket] = (await db.execute(select(Ticket))).scalars().all()
        n_msgs = await db.scalar(select(func.count(TicketMessage.id)).where(TicketMessage.ticket_id == ticket.id))
    assert (ticket.kind, ticket.status, ticket.user_id) == ("appeal", "closed", A2)
    assert n_msgs == 4  # 3 сообщения клиента + ответ команды
    assert any("✅ Разбанен(а): Аня" in t for t in h.texts(A1))

    await h.send(A2, texts.MAIN_MENU["balance"])
    assert h.last_text(A2).startswith("💰 <b>Баланс</b>")


# ---------------------------------------------------------------- 2. поддержка: свайпом в обе стороны
async def test_support_swipe_replies_both_ways(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    await h.send(A2, "/support")
    assert h.last_text(A2).startswith(texts.SUPPORT_PROMPT[:30])
    await h.send(A2, "Как оплатить картой?")
    assert h.last_text(A2) == texts.SUPPORT_SENT
    header, buttons = message_with(h, A1, "💬 Вопрос в поддержку · №")
    assert "Как оплатить картой?" in header.text and buttons[:2] == ["✉️ Ответить", "✅ Закрыть"]

    await h.send(A1, "Через СБП — реквизиты придут со счётом 👍", reply_to=header)
    assert h.last_text(A2) == f"{texts.SUPPORT_FROM_TEAM}\n\nЧерез СБП — реквизиты придут со счётом 👍"
    assert h.last_text(A1).startswith(f"{texts.CHAT_DELIVERED} · По обращению №")
    _, buttons = message_with(h, A1, "💬 Вопрос в поддержку · №")
    assert "✉️ Ответить ещё" in buttons

    await h.send(A2, "А чек нужен?", reply_to=h.last_message(A2))
    assert h.last_text(A2) == texts.SUPPORT_SENT
    assert "· продолжение" in h.last_text(A1) and "А чек нужен?" in h.last_text(A1)

    await h.click(A1, "✉️ Ответить", exact=True)  # v2.3: режим чата с клиентом по обращению
    assert h.last_text(A1).startswith(f"✉️ <b>Вы пишете клиенту @user{A2} по обращению №")
    await h.send(A1, "Да, скрин или PDF")
    assert h.last_text(A2) == f"{texts.SUPPORT_FROM_TEAM}\n\nДа, скрин или PDF"
    assert h.last_text(A1) == texts.CHAT_DELIVERED
    await h.click(A1, "✅ Закрыть")
    assert h.last_text(A1).startswith("✅ Чат с клиентом завершён (По обращению №")  # закрыли — чат завершён
    async with sessionmaker() as db:
        [ticket] = (await db.execute(select(Ticket))).scalars().all()
        dirs = list((await db.execute(select(TicketMessage.direction).where(TicketMessage.ticket_id == ticket.id)
                                      .order_by(TicketMessage.id))).scalars())
    assert ticket.status == "closed" and dirs == ["in", "out", "in", "out"]


# ---------------------------------------------------------------- 3. уровни админов
async def test_admin_levels_invites_permissions_and_removal(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    for uid, name in ((A1, "Аня"), (A2, "Борис"), (A3, "Вера"), (A4, "Глеб")):
        h.user(uid, name)
        await h.send(uid, "/start")

    # владелец приглашает оператора (1 уровень) и старшего (2 уровень) ссылками
    await h.send(A1, "/admin")
    await h.click(A1, "👮 Админы")
    await h.click(A1, "➕ Пригласить: 1 уровень")
    link1 = re.search(r"start=(adm_[\w-]+)", h.last_text(A1)).group(1)
    await h.send(A3, f"/start {link1}")
    assert h.last_text(A3) == texts.ADMIN_INVITE_OK.format(level=roles.LEVELS[1])
    assert config.admin_level(A3) == 1
    assert "принял приглашение" in h.last_text(A1)
    await h.send(A2, f"/start {link1}")  # ссылка одноразовая
    assert config.admin_level(A2) == 0 and any(t.startswith("⚠️") for t in h.texts(A2))

    await h.click(A1, "👮 Админы")
    await h.click(A1, "➕ 2 уровень")
    link2 = re.search(r"start=(adm_[\w-]+)", h.last_text(A1)).group(1)
    await h.send(A4, f"/start {link2}")
    assert config.admin_level(A4) == 2
    async with sessionmaker() as db:
        assert dict((await db.execute(select(Admin.user_id, Admin.level))).all()) == {A3: 1, A4: 2}

    # 1 уровень: заказы, чеки, обращения — без настроек, статистики, выгрузок и админов
    await h.send(A3, "/admin")
    menu = last_kb_labels(h, A3)
    assert any(x.startswith("📥 Заказы") for x in menu) and "👥 Пользователи" in menu
    assert not {"⚙️ Настройки", "📊 Статистика", "📣 Рассылка", "💾 Данные", "👮 Админы"} & set(menu)
    for data in (AdminCb(s="s"), AdminCb(s="m", a="stats"), AdminCb(s="st"), AdminCb(s="u", a="xlsx"),
                 AdminCb(s="d")):
        await press_data(h, A3, data)
        assert h.alerts()[-1] == texts.ADMIN_L2_ONLY, data

    # оператор ведёт заказ целиком; цена вручную и возврат денег — только у 2 уровня
    oid = await wizard_order(h, A2, notes=False)
    _, buttons = message_with(h, A3, f"📥 Новый заказ #{oid}")
    await h.click(A3, "📦 Открыть заказ")
    card = last_kb_labels(h, A3)
    assert "🔢 Кол-во задач" in card and "✏️ Цена" not in card
    await h.click(A3, f"💳 Выставить счёт · {fmt_rub(20000)}")
    await user_pays(h, A2, 20000)
    await h.click_data(A3, AdminCb(s="o", a="open", id=oid).pack())
    assert "↩️ Вернуть деньги" not in last_kb_labels(h, A3)
    await h.click(A3, "🛠 Взять в работу")
    await h.click(A3, "📤 Загрузить решение")
    await h.send(A3, document=("sol-l1", "solution.html", b"<html><body>ok</body></html>"))
    await h.click(A3, "💾 Сохранить (1)")
    await h.click(A3, "📦 Готово, отправить клиенту")
    assert texts.ORDER_DONE.format(id=oid) in h.texts(A2)
    await h.click(A2, texts.DISPUTE_BTN)
    await h.send(A2, photo="remark-l1")
    _, l1_buttons = message_with(h, A3, f"❌ «Не зачли» по заказу #{oid}")
    _, l2_buttons = message_with(h, A1, f"❌ «Не зачли» по заказу #{oid}")
    assert "🔁 Переделать" in l1_buttons and "↩️ Вернуть деньги" not in l1_buttons
    assert "🔁 Переделать" in l2_buttons and "↩️ Вернуть деньги" in l2_buttons
    await h.click(A3, "🔁 Переделать")
    assert texts.REDO.format(id=oid) in h.texts(A2)

    # старший (не владелец) управляет только 1 уровнем: приглашает и снимает
    await h.send(A4, "/admin")
    await h.click(A4, "👮 Админы")
    staff = last_kb_labels(h, A4)
    assert "➕ Пригласить: 1 уровень" in staff and "➕ 2 уровень" not in staff
    await h.click(A4, "🛡 Глеб")  # себя снять нельзя
    assert "🗑 Снять доступ" not in last_kb_labels(h, A4)
    await h.click(A4, "⬅️ К админам")
    await h.click(A4, "🔹 Вера")
    card = last_kb_labels(h, A4)
    assert "🗑 Снять доступ" in card and "⬆️ Сделать 2 уровнем" not in card  # повышать может только владелец
    await h.click(A4, "🗑 Снять доступ")
    await h.click(A4, "🗑 Да, снять")
    assert h.last_text(A3) == texts.ADMIN_REMOVED_NOTICE
    assert config.admin_level(A3) == 0
    await h.send(A3, "/admin")
    assert h.last_text(A3) == texts.ADMIN_ONLY

    await h.send(A1, "/admin")  # владелец может понизить старшего
    await h.click(A1, "👮 Админы")
    await h.click(A1, "🛡 Глеб")
    await h.click(A1, "⬇️ Сделать 1 уровнем")
    assert config.admin_level(A4) == 1
    async with sessionmaker() as db:
        assert dict((await db.execute(select(Admin.user_id, Admin.level))).all()) == {A4: 1}
        assert await integrity_issues(db) == []


# ---------------------------------------------------------------- 6. объяснение материала по фото
async def explain_request(h, uid: int, question: str | None) -> int:  # noqa: ANN001
    await h.send(uid, texts.MAIN_MENU["explain"])
    assert texts.EXPLAIN_TITLE in h.last_text(uid)
    await h.send(uid, photo=f"lecture-{uid}")
    await h.click(uid, "✅ Готово")
    assert texts.STEP_EXPLAIN_QUESTION in h.last_text(uid)
    if question is None:
        await h.click(uid, texts.BTN_EXPLAIN_ALL)
    else:
        await h.send(uid, question)
    await h.click(uid, texts.DEADLINE_BTNS["d3"])
    day = local_today() + timedelta(days=4)
    await h.click(uid, f"{WEEKDAYS[day.weekday()]} {day:%d.%m}")
    assert texts.EXPLAIN_SUMMARY_TITLE in h.last_text(uid)
    await h.click(uid, texts.SUBMIT_BTN)
    _, markup = h.screen(uid)[-1]
    if markup and any(b.text == texts.TERMS_ACCEPT_BTN for row in markup.inline_keyboard for b in row):
        await h.click(uid, texts.TERMS_ACCEPT_BTN)
    return int(re.search(r"Заявка #(\d+) отправлена", h.last_text(uid)).group(1))


async def test_explain_material_end_to_end(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    oid = await explain_request(h, A2, "Не понимаю, как строить линию пересечения плоскостей")
    assert h.last_text(A2) == texts.EXPLAIN_SUBMITTED.format(id=oid)
    order = await get(sessionmaker, Order, oid)
    assert (order.subject, order.status, order.price) == ("explain", "review", 15000)
    assert order.comment == "Не понимаю, как строить линию пересечения плоскостей"
    assert any(t.startswith(f"📸 Заявка на объяснение #{oid}") for t in h.texts(A1))

    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    assert "🔢 Кол-во задач" not in last_kb_labels(h, A1)  # у объяснения нет задач
    await h.click(A1, f"💳 Выставить счёт · {fmt_rub(15000)}")
    await user_pays(h, A2, 15000)
    assert (await get(sessionmaker, Order, oid)).status == "paid"

    # объяснение пишется прямо клиенту в чате по заказу (v2.3: «✉️ Ответить»); первое сообщение само берёт
    # заявку в работу
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    await h.click(A1, texts.CHAT_BTN)
    assert f"Вы пишете клиенту @user{A2} по заказу №{oid}" in h.last_text(A1)
    head = texts.CHAT_TO_USER.format(mark=texts.CHAT_MARK_ORDER.format(id=oid))
    await h.send(A1, "Смотри: сначала строим две вспомогательные плоскости…")
    assert h.last_text(A2) == f"{head}\n\nСмотри: сначала строим две вспомогательные плоскости…"
    await h.send(A1, photo="explain-drawing")
    order = await get(sessionmaker, Order, oid)
    assert order.status == "in_work" and order.assignee_id == A1 and order.chat_replies == 2
    assert h.last_text(A1) == texts.CHAT_DELIVERED
    await h.click(A1, texts.CHAT_READY_BTN)  # «📦 Готово — отправить клиенту» прямо из чата
    assert h.last_text(A2) == texts.EXPLAIN_DONE.format(id=oid)
    assert (await get(sessionmaker, Order, oid)).status == "done"

    # остался вопрос — «💬 Задать вопрос», ответ свайпом приходит с заголовком объяснения
    await h.click(A2, texts.EXPLAIN_ASK_BTN)
    assert h.last_text(A2) == texts.ORDER_QUESTION_PROMPT.format(id=oid)
    await h.send(A2, "А вторую плоскость как выбрать?")
    assert h.last_text(A2) == texts.SUPPORT_SENT
    header, _ = message_with(h, A1, f"💬 Вопрос по заказу #{oid} · №")
    await h.send(A1, "Любую, параллельную оси", reply_to=header)
    assert h.last_text(A2) == f"{head}\n\nЛюбую, параллельную оси"

    await h.click(A2, texts.EXPLAIN_ACCEPT_BTN)
    assert texts.EXPLAIN_ACCEPTED in h.texts(A2)
    assert (await get(sessionmaker, Order, oid)).status == "accepted"
    assert any(f"🎓 Объяснение #{oid}" in t for t in h.texts(A1))

    # «объясните всю тему» без вопроса и «Не зачли» у объяснения нет
    oid2 = await explain_request(h, A2, None)
    assert (await get(sessionmaker, Order, oid2)).comment
    await h.send(A2, texts.MAIN_MENU["my_orders"])
    await h.click(A2, f"#{oid} ·")
    assert texts.DISPUTE_BTN not in last_kb_labels(h, A2)
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []


# ---------------------------------------------------------------- 7. обучалка
async def test_tutorial_onboarding(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    assert not any(t.startswith(GUIDE_HEAD) for t in h.texts(A1))  # админам обучалка сама не показывается

    await h.send(A2, "/start")
    assert h.last_text(A2).startswith(f"{GUIDE_HEAD} · 1/5")
    assert texts.MAIN_MENU["explain"] in h.last_text(A2)
    for page in range(2, 6):
        await h.click(A2, texts.GUIDE_NEXT)
        assert h.last_text(A2).startswith(f"{GUIDE_HEAD} · {page}/5")
        if page == 2:
            h.find_button(A2, texts.MAIN_MENU["order"])
        if page == 3:
            h.find_button(A2, texts.MAIN_MENU["explain"])
    page5 = last_kb_labels(h, A2)
    assert texts.CHANNEL_CHECK_BTN in page5 and texts.GUIDE_DONE in page5 and texts.GUIDE_SKIP not in page5
    _, open_btn = h.find_button(A2, texts.CHANNEL_OPEN_BTN)
    assert open_btn.url == "https://t.me/aegis_samara"
    assert "@aegis_samara" in h.last_text(A2) and fmt_rub(5000) in h.last_text(A2)
    await h.click(A2, texts.GUIDE_PREV)
    page4 = h.last_text(A2)
    assert "· 4/5" in page4 and "×1,5" in page4 and "×2,0" in page4 and "За 2+ дня до дедлайна: без наценки" in page4
    await h.click(A2, texts.GUIDE_SKIP)
    assert h.last_text(A2) == texts.GUIDE_FINISHED
    assert (await get(sessionmaker, User, A2)).tutorial_done_at is not None

    n = len(h.texts(A2))
    await h.send(A2, "/start")
    assert not any(t.startswith(GUIDE_HEAD) for t in h.texts(A2)[n:])  # пройдено — больше не навязываем
    await h.send(A2, "/guide")
    assert h.last_text(A2).startswith(f"{GUIDE_HEAD} · 1/5")
    await h.send(A2, texts.MAIN_MENU["help"])
    await h.click(A2, texts.GUIDE_BTN)
    assert h.last_text(A2).startswith(f"{GUIDE_HEAD} · 1/5")
    await h.click(A2, texts.GUIDE_NEXT)
    await h.click(A2, texts.MAIN_MENU["order"])  # кнопка со страницы обучалки сразу начинает заказ
    assert any(texts.SUBJECTS["nach"] in x for x in last_kb_labels(h, A2))


# ---------------------------------------------------------------- 8. канал: бонус за подписку и за друга
async def test_channel_bonus_referral_and_anti_abuse(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    for uid, name in ((A1, "Аня"), (A2, "Борис"), (A3, "Вера")):
        h.user(uid, name)
    await start_with_referral(h, A1, A2)
    assert texts.REF_NEW_FRIEND in h.texts(A1)
    invite = next(t for t in reversed(h.texts(A1)) if t.startswith(texts.INVITE_TITLE))
    assert "@aegis_samara" in invite and fmt_rub(5000) in invite

    # друг подписался → ему +50 сразу; пригласившему — после 48 ч удержания и только если он сам подписан
    await h.channel(A2, True)
    assert h.last_text(A2).startswith(f"🎉 <b>Спасибо за подписку на @aegis_samara!</b>\n\nТебе <b>+{R50} бонусами</b>")
    assert (await get(sessionmaker, User, A2)).balance_bonus == 5000
    assert h.last_text(A1).startswith(f"👥 Твой друг Борис подписался на канал! Чтобы получить +{R50}")
    await h.click(A1, texts.CHANNEL_CHECK_BTN)  # ещё не подписан
    assert h.alerts()[-1] == texts.CHANNEL_NOT_MEMBER.format(channel="@aegis_samara")
    await h.channel(A1, True)
    assert h.last_text(A1).startswith("🎉 <b>Спасибо за подписку на @aegis_samara!</b>")
    await press_data(h, A2, MenuCb(action="ch_check"))  # старая кнопка «проверить» — без второго бонуса
    assert h.alerts()[-1].startswith("✅ Подписка есть, бонус за неё уже начислен")
    assert (await get(sessionmaker, User, A2)).balance_bonus == 5000

    later = now_utc() + timedelta(hours=49)
    assert await scheduler.channel_watch(h.bot, sessionmaker, now=later) == 1
    assert h.last_text(A1).startswith(f"🎉 +{R50} бонусами за друга Борис: он подписан на канал.")
    assert await scheduler.channel_watch(h.bot, sessionmaker, now=later + timedelta(minutes=15)) == 0
    assert (await get(sessionmaker, User, A1)).balance_bonus == 10000

    # накрутка: подписался → получил бонус → отписался в течение 48 ч → бонус списан навсегда
    await h.send(A1, texts.MAIN_MENU["invite"])
    ref = re.search(r"start=(ref_[\w-]+)", h.last_text(A1)).group(1)
    await h.send(A3, f"/start {ref}")
    await h.channel(A3, True)
    assert (await get(sessionmaker, User, A3)).balance_bonus == 5000
    await h.channel(A3, False)
    assert h.last_text(A3).startswith("😔 Ты отписался от @aegis_samara раньше чем через 48 ч")
    assert (await get(sessionmaker, User, A3)).balance_bonus == 0
    n = len(h.texts(A3))
    await h.channel(A3, True)  # вернулся — второй раз не начисляем
    assert len(h.texts(A3)) == n
    await h.send(A3, texts.MAIN_MENU["gifts"])
    assert texts.CHANNEL_CHECK_BTN not in last_kb_labels(h, A3)  # кнопки «проверить» больше нет
    await press_data(h, A3, MenuCb(action="ch_check"))
    assert h.alerts()[-1] == texts.CHANNEL_REVOKED_BEFORE
    assert (await get(sessionmaker, User, A3)).balance_bonus == 0
    assert await scheduler.channel_watch(h.bot, sessionmaker, now=later + timedelta(hours=1)) == 0
    async with sessionmaker() as db:
        subs = dict((await db.execute(select(Referral.referred_id, Referral.sub_status))).all())
        assert subs == {A2: "rewarded", A3: "cancelled"}
        assert await integrity_issues(db) == []


async def test_channel_referral_daily_limit_and_check_failure(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await set_settings(sessionmaker, channel_ref_daily_limit=1)
    for uid, name in ((A1, "Аня"), (A2, "Борис"), (A3, "Вера"), (A4, "Глеб")):
        h.user(uid, name)
    await start_with_referral(h, A1, A2)
    await h.send(A1, texts.MAIN_MENU["invite"])
    ref = re.search(r"start=(ref_[\w-]+)", h.last_text(A1)).group(1)
    await h.send(A3, f"/start {ref}")
    await h.channel(A1, True)
    await h.channel(A2, True)
    await h.channel(A3, True)
    assert h.last_text(A1).startswith(f"👥 Твой друг Вера подписался на канал! Тебе +{R50} бонусами придут")

    # лимит — на календарные сутки (Самара): берём полдень, чтобы «+1 ч» не перескочил через полночь
    later = datetime.combine(local_today() + timedelta(days=3), time(12), tzinfo=TZ).astimezone(timezone.utc)
    assert await scheduler.channel_watch(h.bot, sessionmaker, now=later) == 1  # не больше 1 друга в сутки
    assert await scheduler.channel_watch(h.bot, sessionmaker, now=later + timedelta(hours=1)) == 0
    assert await scheduler.channel_watch(h.bot, sessionmaker, now=later + timedelta(days=1, hours=1)) == 1
    assert (await get(sessionmaker, User, A1)).balance_bonus == 5000 + 2 * 5000

    # бот не админ канала: клиенту — «попробуй позже», старшим админам — предупреждение
    h.fake.channel_error = "Bad Request: member list is inaccessible"
    await h.send(A4, texts.MAIN_MENU["gifts"])
    await h.click(A4, texts.CHANNEL_CHECK_BTN)
    assert h.alerts()[-1] == texts.CHANNEL_CHECK_FAILED
    assert h.last_text(A1).startswith("⚠️ Не получается проверить подписку на @aegis_samara")
    await h.send(A1, "/admin")
    assert "⚠️ Не проверяется подписка на @aegis_samara" in h.last_text(A1)
    assert (await get(sessionmaker, User, A4)).balance_bonus == 0


# ---------------------------------------------------------------- 3. оплата переводом: чек → админ подтверждает
async def test_manual_transfer_reject_then_confirm_by_operator(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    details = "Карта 2200 0000 0000 0000 (Т-Банк), получатель Иван И."
    await set_settings(sessionmaker, payments_mode="manual", manual_pay_details=details)
    for uid, name in ((A1, "Аня"), (A2, "Борис"), (A3, "Вера")):
        h.user(uid, name)
        await h.send(uid, "/start")
    async with sessionmaker() as db:
        await roles.set_level(db, config, A3, 1, A1, now_utc())  # оператор 1 уровня

    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")
    assert details in h.last_text(A2) and "К оплате: <b>" in h.last_text(A2)
    await h.click(A2, texts.MANUAL_CLAIM_BTN)
    assert h.last_text(A2).startswith("📎 Пришли скрин или PDF-чек")
    await h.send(A2, "вот")  # не файл — подсказка, ждём чек дальше
    assert "Отправить без чека" in h.last_text(A2)
    await h.send(A2, photo="receipt-1")
    assert h.last_text(A2).startswith("✅ <b>Спасибо! Чек получили</b>")
    for admin in (A1, A3):  # чек — у всех админов, и у 1 уровня тоже
        _, buttons = message_with(h, admin, "🧾 Чек об оплате · счёт №")
        assert buttons[:2] == ["✅ Оплата пришла", "❌ Не пришла"]
    photos = [m for m in h.calls("SendPhoto") if m.chat_id == A3]
    assert photos and photos[-1].photo == "receipt-1"

    # чек висит больше часа — одно напоминание
    later = now_utc() + timedelta(hours=2)
    assert await scheduler.remind_receipts(h.bot, config, sessionmaker, now=later) == 1
    assert h.last_text(A1).startswith("⏰ <b>Чеки ждут проверки больше часа</b>")
    assert await scheduler.remind_receipts(h.bot, config, sessionmaker, now=later) == 0

    await h.click(A3, "❌ Не пришла")
    await h.click(A3, "❌ Да, не пришли")
    assert h.last_text(A2).startswith("❌ Мы не нашли поступление по счёту №")
    assert (await get(sessionmaker, Order, oid)).status == "awaiting_payment"
    assert any("❌ Поступление не найдено: Вера" in t for t in h.texts(A1))

    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")  # оплатил снова, без чека
    await h.click(A2, texts.MANUAL_CLAIM_BTN)
    await h.click(A2, texts.MANUAL_NO_RECEIPT_BTN)
    assert h.last_text(A2).startswith("✅ <b>Спасибо! Чек получили</b>")
    _, buttons = message_with(h, A3, "🧾 Чек об оплате · счёт №")
    await h.click(A3, "✅ Оплата пришла")
    await h.click(A3, "✅ Да, деньги пришли")
    assert texts.PAY_DONE_ORDER.format(id=oid) in h.texts(A2)
    order = await get(sessionmaker, Order, oid)
    assert order.status == "paid" and not order.is_test and order.paid_invoice == 20000
    async with sessionmaker() as db:
        pays = list((await db.execute(select(Payment.id, Payment.provider, Payment.status)
                                      .where(Payment.order_id == oid).order_by(Payment.id))).all())
        assert [(p, st) for _, p, st in pays] == [("manual", "rejected"), ("manual", "paid")]
        assert await integrity_issues(db) == []
    await press_data(h, A1, AdminCb(s="pay", a="ok_go", id=pays[-1][0]))  # второй админ опоздал — без двойного зачёта
    assert h.alerts()[-1]
    assert (await get(sessionmaker, Order, oid)).paid_invoice == 20000
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []


# ---------------------------------------------------------------- 4. телефон и Excel для 2 уровня
async def test_phone_and_users_excel(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    await h.send(A2, texts.MAIN_MENU["help"])
    await h.click(A2, "📱 Оставить номер")
    assert h.last_text(A2) == texts.PHONE_PROMPT
    await h.send(A2, contact=999)  # чужой контакт не принимаем
    assert h.last_text(A2) == texts.PHONE_NOT_OWN
    await h.send(A2, contact=A2)
    assert h.last_text(A2) == texts.PHONE_SAVED
    assert (await get(sessionmaker, User, A2)).phone == "+79991234567"

    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    await user_pays(h, A2, 20000)

    await h.send(A1, "/admin")
    await h.click(A1, "👥 Пользователи")
    await h.click(A1, "📊 Excel-таблица")
    [doc] = [d for d in h.sent_documents(A1) if (d.caption or "").startswith("📊 Пользователи")]
    [path] = list(config.exports_dir.glob("aegis-users-*.xlsx"))
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Пользователи", "Покупки", "Пополнения"]

    def table(name: str) -> list[dict]:
        rows = list(wb[name].iter_rows(values_only=True))
        return [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]

    users = {u["ID"]: u for u in table("Пользователи")}
    assert set(users) == {A1, A2}
    assert (users[A2]["Телефон"], users[A2]["Username"], users[A2]["Оплачено заказов"]) == ("+79991234567", "@user2002", 1)
    assert users[A2]["Что купил последним"] == f"#{oid} Начерталка" and users[A1]["Телефон"] in ("", None)
    [buy] = table("Покупки")
    assert (buy["Заказ"], buy["Телефон"], buy["Что"], buy["Цена, ₽"], buy["Тест"]) == (
        oid, "+79991234567", "Начерталка", 200, "да")
    async with sessionmaker() as db:
        from app.db.models import AdminLog  # noqa: PLC0415
        assert await db.scalar(select(func.count(AdminLog.id)).where(AdminLog.action == "export_users_xlsx")) == 1


# ---------------------------------------------------------------- 9. наценка за срочность — в кнопках и итоге
async def test_urgency_hints_follow_settings(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await set_settings(sessionmaker, urgency_nach=True)  # v2.4: по умолчанию у начерталки наценки нет, включаем
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")

    async def to_deadline_step() -> None:
        await h.send(A2, texts.MAIN_MENU["order"])
        if h.last_text(A2).startswith(texts.DRAFT_SCREEN.split("\n")[0]):  # v2.3: черновик ждёт — начнём заново
            await h.click(A2, texts.DRAFT_RESTART)
            await h.click(A2, texts.DRAFT_RESTART)  # подтверждение: старый черновик удалится
        await h.click(A2, texts.SUBJECTS["nach"])
        await h.send(A2, photo=f"cond-urg-{len(h.texts(A2))}")
        await h.click(A2, "✅ Готово")
        await h.click(A2, "1", exact=True)
        await h.click(A2, texts.BTN_NO_VARIANT)

    await to_deadline_step()
    step = h.last_text(A2)
    assert "⏰ За 2+ дня до дедлайна: без наценки" in step and "⚡ За 1-2 дня: ×1,5" in step
    assert "🔥 Меньше суток: ×2,0" in step
    kb = last_kb_labels(h, A2)
    assert "Завтра · ×1,5" in kb and "Через 2 дня · без наценки" in kb and "Через 3+ дня · без наценки" in kb
    assert all(x == "Сегодня · ×2,0" for x in kb if x.startswith("Сегодня"))  # поздно вечером «Сегодня» нет
    await h.click(A2, "Завтра")  # v2.4: шагов конспектов нет (скидки выключены)
    await h.click(A2, "⏭ Без комментария")
    summary = h.last_text(A2)
    assert f"срочность ×1,5 = <b>{fmt_rub(30000)}</b>" in summary

    # 2 уровень меняет пороги и множители — тексты и подсказки меняются сразу
    await h.send(A1, "/admin")
    await h.click(A1, "⚙️ Настройки")
    await h.click(A1, "⏱ Срочность")
    assert "Так видят клиенты" in h.last_text(A1)
    await h.click(A1, "Множитель «скоро»")
    await h.send(A1, "1,3")
    assert h.last_text(A1).startswith("✅ Множитель «скоро»: ×1,3")
    await h.click(A1, "Без наценки, если до дедлайна от")
    await h.send(A1, "72")
    await set_settings(sessionmaker, urgency_lt24_pct=180)
    await to_deadline_step()
    step = h.last_text(A2)
    assert "⏰ За 3+ дня до дедлайна: без наценки" in step and "⚡ За 1-3 дня: ×1,3" in step
    assert "🔥 Меньше суток: ×1,8" in step
    kb = last_kb_labels(h, A2)
    assert "Завтра · ×1,3" in kb and "Через 2 дня · ×1,3" in kb
    await h.send(A2, texts.MAIN_MENU["help"])
    await h.click(A2, "⏰ Сроки и цены")
    assert "⚡ За 1-3 дня: ×1,3" in h.last_text(A2)

    # неправильные пороги не сохраняются
    await h.click(A1, "Максимальная наценка, если меньше")
    await h.send(A1, "100")
    assert h.last_text(A1).startswith("⚠️ Должно быть меньше порога «без наценки» (72 ч)")
    async with sessionmaker() as db:
        s = await load_settings(db)
    assert (s.urgency_free_hours, s.urgency_mid_hours, s.urgency_24_72_pct) == (72, 24, 130)


# ---------------------------------------------------------------- 5. без дублей: повторное нажатие не плодит вопросы
async def test_admin_prompts_are_not_duplicated(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    oid = await wizard_order(h, A2, notes=False)
    await h.click_data(A1, AdminCb(s="o", a="open", id=oid).pack())
    card_msg, price_btn = h.find_button(A1, "✏️ Цена")
    sent_before = len(h.calls("SendMessage"))
    await h.press(A1, card_msg, price_btn)
    await h.press(A1, card_msg, price_btn)  # двойное нажатие / старая кнопка
    assert len(h.calls("SendMessage")) == sent_before  # вопрос — на месте карточки, не новым сообщением
    assert h.last_text(A1).startswith(f"✏️ <b>Цена заказа #{oid}</b>")
    await h.send(A1, "250")
    assert (await get(sessionmaker, Order, oid)).price == 25000
    deleted = [m for m in h.calls("DeleteMessage") if m.chat_id == A1]
    assert deleted and deleted[-1].message_id == card_msg.message_id  # вопрос убран после ответа
    assert "✏️ Цена: 250" in h.last_text(A1)
    h.find_button(A1, f"💳 Выставить счёт · {fmt_rub(25000)}")

    await h.click(A1, "🔢 Кол-во задач")
    await h.click(A1, "✖️ Отмена")  # отмена возвращает тот же экран на месте
    assert f"#{oid}" in h.last_text(A1) and "💳 Выставить счёт" in " ".join(last_kb_labels(h, A1))


# ---------------------------------------------------------------- 10. напоминания и сводка
async def test_reminders_deadline_alerts_and_digest(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    h.user(A1, "Аня")
    h.user(A2, "Борис")
    await h.send(A1, "/start")
    await h.send(A2, "/start")
    assert not await scheduler.admin_digest(h.bot, config, sessionmaker)  # делать нечего — не пишем

    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    now = now_utc()
    assert await scheduler.remind_unpaid(h.bot, sessionmaker, now=now + timedelta(hours=1)) == 0
    assert await scheduler.remind_unpaid(h.bot, sessionmaker, now=now + timedelta(hours=7)) == 1
    assert h.last_text(A2) == texts.PAY_REMINDER.format(id=oid, price=fmt_rub(20000))
    h.find_button(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")
    assert await scheduler.remind_unpaid(h.bot, sessionmaker, now=now + timedelta(hours=30)) == 0  # один раз

    oid2 = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid2, 20000)
    async with sessionmaker() as db:
        (await db.get(Order, oid2)).deadline_at = now + timedelta(hours=5)
        await db.commit()
    assert await scheduler.remind_unpaid(h.bot, sessionmaker, now=now) == 1
    assert h.last_text(A2).startswith(f"⏰ Заказ #{oid2} ждёт оплаты, а до дедлайна осталось")

    await user_pays(h, A2, 20000)  # оплачен первый счёт в списке — заказ oid
    paid = oid if (await get(sessionmaker, Order, oid)).status == "paid" else oid2
    async with sessionmaker() as db:
        (await db.get(Order, paid)).deadline_at = now + timedelta(hours=2)
        await db.commit()
    assert await scheduler.alert_deadlines(h.bot, config, sessionmaker, now=now) >= 1
    assert any(t.startswith(f"⏰ <b>Дедлайн близко</b>: заказ #{paid}") for t in h.texts(A1))
    assert await scheduler.alert_deadlines(h.bot, config, sessionmaker, now=now) == 0

    assert await scheduler.admin_digest(h.bot, config, sessionmaker, now=now)
    assert h.last_text(A1).startswith("☀️ <b>Доброе утро! Сводка на сегодня</b>")
    h.find_button(A1, "🛡 Открыть админку")


async def test_startup_checks_bot_is_channel_admin(config) -> None:
    bot, fake = make_bot()
    try:
        assert await chsvc.bot_rights_problem(bot, "@aegis_samara") == "бот не администратор канала @aegis_samara"
        assert chsvc.failing()  # в админке появится предупреждение
        fake.channel_admins.add(bot.id)
        assert await chsvc.bot_rights_problem(bot, "@aegis_samara") is None and not chsvc.failing()
        fake.channel_error = "Bad Request: chat not found"
        assert "недоступен боту" in await chsvc.bot_rights_problem(bot, "@aegis_samara")
        assert await chsvc.bot_rights_problem(bot, "") is None  # канал не задан — проверять нечего
    finally:
        await bot.session.close()
