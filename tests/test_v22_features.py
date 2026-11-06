"""Новое в 2.2: закреплённые админы 2 уровня из .env, права 1/2 уровня на сервере, команды по ролям,
выдача и списание денег, бан / приоритет / «Месяц бесплатно» по @username, запросы к БД,
задания «📌 Ссылка в профиле» и «📣 Пост о нас» (с отчётами о просмотрах)."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import scheduler, texts
from app.bot import pinned as pinned_glue
from app.bot.callbacks import AdminCb, MenuCb, PromoCb
from app.bot.commands import ADMIN_COMMAND_NAMES, L2_COMMAND_NAMES, set_commands
from app.bot.handlers.promo import plain
from app.db.models import Admin, AdminLog, Order, PinnedAdmin, ProfileClaim, PromoPost, PromoPostReport, Transaction, User
from app.services import admin_money, roles, stats
from app.services.maintenance import integrity_issues, ledger_totals
from app.utils.money import INT64_MAX, fmt_rub
from app.utils.timeutil import now_utc
from tests.conftest import BOT_USERNAME
from tests.scenario import A1, A2, admin_invoice, get, set_settings, wizard_order
from tests.test_v2_features import last_kb_labels, press_data

A3, A4, A5 = 3003, 4004, 5005
BORIS = "Борис (@user2002, id <code>2002</code>)"  # display_name второго аккаунта
USER_NAMES = [c for c, _ in texts.COMMANDS_USER]
L1_NAMES = USER_NAMES + [c for c, _ in texts.COMMANDS_ADMIN_L1]
L2_NAMES = L1_NAMES + [c for c, _ in texts.COMMANDS_ADMIN_L2]


@pytest.fixture(autouse=True)
def _fresh_promo_albums() -> None:
    from app.bot.handlers import promo

    promo._albums.clear()  # noqa: SLF001


# ---------------------------------------------------------------- помощники
async def start(h, *people: tuple[int, str]) -> None:  # noqa: ANN001
    for uid, name in people:
        h.user(uid, name)
        await h.send(uid, "/start")


async def set_level(sessionmaker, config, uid: int, level: int) -> None:  # noqa: ANN001
    async with sessionmaker() as db:
        await roles.set_level(db, config, uid, level, A1, now_utc())


def menu_of(h, chat_id: int) -> list[str] | None:  # noqa: ANN001
    """Последний личный список команд (setMyCommands для чата chat_id) — имена команд."""
    for m in reversed(h.calls("SetMyCommands")):
        if getattr(m.scope, "chat_id", None) == chat_id:
            return [c.command for c in m.commands]
    return None


def all_labels(h, uid: int) -> list[str]:  # noqa: ANN001
    return [b.text for _, kb in h.screen(uid) if kb for row in kb.inline_keyboard for b in row]


async def balance(sessionmaker, uid: int) -> int:  # noqa: ANN001
    return (await get(sessionmaker, User, uid)).balance_rub


async def log_actions(sessionmaker, *actions: str) -> int:  # noqa: ANN001
    async with sessionmaker() as db:
        return int(await db.scalar(select(func.count(AdminLog.id)).where(AdminLog.action.in_(actions))) or 0)


async def check_integrity(sessionmaker) -> None:  # noqa: ANN001
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []


# ---------------------------------------------------------------- 1. закреплённые админы 2 уровня
async def test_pinned_senior_admin_binds_by_id_and_spoof_gets_nothing(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"))
    assert "badvinq" in config.senior_usernames and "dimaslavikov1" in config.senior_usernames  # значения по умолчанию

    # человек с ником из SENIOR_ADMIN_USERNAMES пишет боту → 2 уровень, привязка ника к его id
    h.user(A2, "Борис", username="badVInq")
    await h.send(A2, "/start")
    assert config.admin_level(A2) == 2 and config.is_pinned(A2)
    assert texts.PINNED_WELCOME in h.texts(A2)
    assert texts.PINNED_BOUND.format(who="@badVInq", id=A2) in h.texts(A1)
    assert menu_of(h, A2) == L2_NAMES
    async with sessionmaker() as db:
        row = await db.scalar(select(PinnedAdmin))
        assert (row.username, row.user_id, row.source, row.revoked_at) == ("badvinq", A2, "username", None)
        admin = await db.get(Admin, A2)
        assert (admin.level, admin.pinned) == (2, True)
    await h.send(A2, "/admin")
    assert texts.MANAGE_BTN in last_kb_labels(h, A2)
    bound_alerts = h.texts(A1).count(texts.PINNED_BOUND.format(who="@badVInq", id=A2))
    await h.send(A2, "/start")  # повторный заход — без повторных уведомлений
    assert h.texts(A1).count(texts.PINNED_BOUND.format(who="@badVInq", id=A2)) == bound_alerts

    # тот же ник с другого id (подмена или ник сменил владельца) — прав нет, владельцу одно предупреждение
    h.user(A3, "Фейк", username="BADVINQ")
    await h.send(A3, "/start")
    await h.send(A3, "/admin")
    assert config.admin_level(A3) == 0 and h.last_text(A3) == texts.ADMIN_ONLY
    await h.send(A3, "/give @badVInq 1000000")
    assert h.last_text(A3) == texts.ADMIN_CMD_ONLY
    spoof = [t for t in h.texts(A1) if t.startswith("⚠️ <b>Попытка подмены")]
    assert len(spoof) == 1 and f"<code>{A2}</code>" in spoof[0] and f"<code>{A3}</code>" in spoof[0]
    assert await log_actions(sessionmaker, "pinned_spoof") == 1

    # права держатся за id: Борис сменил ник — остаётся админом
    h.user(A2, "Борис", username="boris_new")
    await h.send(A2, "/admin")
    assert config.admin_level(A2) == 2 and texts.MANAGE_BTN in last_kb_labels(h, A2)

    # старший (не владелец) закреплённого не снимает и не понижает
    await start(h, (A4, "Глеб"))
    await set_level(sessionmaker, config, A4, 2)
    await h.send(A4, "/admin")
    await h.click(A4, "👮 Админы")
    await h.click(A4, "📌 Борис")
    assert not {"🗑 Снять доступ", "⬇️ Сделать 1 уровнем"} & set(last_kb_labels(h, A4))
    await h.send(A4, "/removeadmin @boris_new")
    assert h.last_text(A4).startswith("⛔ Недостаточно прав")
    await press_data(h, A4, AdminCb(s="st", a="rm_go", id=A2))
    assert h.alerts()[-1].startswith("Недостаточно прав")
    assert config.admin_level(A2) == 2

    # владелец понижает — закрепление снимается и по нику больше не выдаётся; вернуть — кнопкой
    await h.send(A1, "/admin")
    await h.click(A1, "👮 Админы")
    await h.click(A1, "📌 Борис")
    await h.click(A1, "⬇️ Сделать 1 уровнем")
    assert config.admin_level(A2) == 1 and not config.is_pinned(A2)
    assert menu_of(h, A2) == L1_NAMES
    h.user(A2, "Борис", username="badVInq")  # снова с ником из .env — права по нику уже не возвращаются
    await h.send(A2, "/start")
    assert config.admin_level(A2) == 1 and not config.is_pinned(A2)
    await h.click(A1, "⬅️ К админам")
    await h.click(A1, "📌 Вернуть закрепление: @badVInq")
    assert config.admin_level(A2) == 2 and config.is_pinned(A2)
    assert h.texts(A2).count(texts.PINNED_WELCOME) == 2 and menu_of(h, A2) == L2_NAMES

    # ник убрали из .env → при запуске закрепление снимается; вернули → восстанавливается при сообщении
    config.senior_admin_usernames_raw = "dimaslavikov1"
    await pinned_glue.startup_sync(h.bot, config, sessionmaker)
    assert config.admin_level(A2) == 0 and h.last_text(A2) == texts.ADMIN_REMOVED_NOTICE
    assert any(t.startswith("📌 Закрепление снято: @badvinq") for t in h.texts(A1))
    config.senior_admin_usernames_raw = "badVInq,dimaslavikov1"
    await pinned_glue.startup_sync(h.bot, config, sessionmaker)
    h.user(A2, "Борис", username="badvinq")
    await h.send(A2, "/start")
    assert config.admin_level(A2) == 2 and config.is_pinned(A2)

    # SENIOR_ADMIN_IDS: закрепление по id при запуске, без ника; убрали id — доступ снят
    config.senior_admin_ids_raw = str(A5)
    await pinned_glue.startup_sync(h.bot, config, sessionmaker)
    assert config.admin_level(A5) == 2 and config.is_pinned(A5)
    config.senior_admin_ids_raw = ""
    await pinned_glue.startup_sync(h.bot, config, sessionmaker)
    assert config.admin_level(A5) == 0
    assert any("SENIOR_ADMIN_IDS" in t for t in h.texts(A1) if t.startswith("📌 Закрепление снято"))
    async with sessionmaker() as db:
        assert dict((await db.execute(select(Admin.user_id, Admin.level))).all()) == {A2: 2, A4: 2}
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- 2. права: сервер проверяет каждое нажатие и команду
L2_BUTTONS = (AdminCb(s="g"), AdminCb(s="vip"), AdminCb(s="vip", a="pr", id=A2),
              AdminCb(s="vip", a="pr_go", id=A2, v="on"), AdminCb(s="vip", a="fm_go", id=A2, v="30.10"),
              AdminCb(s="bn"), AdminCb(s="bn", a="ask", id=A2, v="spam"), AdminCb(s="bn", a="go", id=A2, v="spam"),
              AdminCb(s="bn", a="unban_go", id=A2), AdminCb(s="gv"), AdminCb(s="gv", a="u", id=A2),
              AdminCb(s="gv", a="ok", id=A2, v="50000"), AdminCb(s="gv", a="big", id=A2, v="5000000"),
              AdminCb(s="db"), AdminCb(s="db", a="file"), AdminCb(s="db", a="money"),
              AdminCb(s="db", a="user", id=A2), AdminCb(s="u", a="adj", id=A2, v="rub+"),
              AdminCb(s="u", a="prio", id=A2), AdminCb(s="u", a="fm", id=A2), AdminCb(s="pt", a="adj", id=1),
              AdminCb(s="pt", a="adj_go", id=1), AdminCb(s="st"), AdminCb(s="st", a="grant", id=A2, v="1"),
              AdminCb(s="d", a="csv", v="db"), AdminCb(s="u", a="xlsx", v="db"), AdminCb(s="s", a="grp", v="tasks"))


async def test_level1_and_users_cannot_use_level2_buttons_or_commands(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"))
    await set_level(sessionmaker, config, A3, 1)

    # 1 уровень: в админке нет блока управления, есть «🎯 Задания»
    await h.send(A3, "/admin")
    menu = last_kb_labels(h, A3)
    assert any(x.startswith(texts.TASKS_BTN) for x in menu) and "👥 Пользователи" in menu
    assert not {texts.MANAGE_BTN, "👮 Админы", "⭐ Приоритет / 🎟 Месяц", "🚫 Бан / разбан", "💸 Деньги",
                "🗄 Запросы к БД", "⚙️ Настройки", "💾 Данные"} & set(menu)
    for data in L2_BUTTONS:  # подделанные или старые кнопки 2 уровня
        await press_data(h, A3, data)
        assert h.alerts()[-1] == texts.ADMIN_L2_ONLY, data
    for name in L2_COMMAND_NAMES:  # команды 2 уровня, набранные руками
        await h.send(A3, f"/{name} @user2002 500")
        assert h.last_text(A3) == texts.ADMIN_L2_ONLY, name
        await h.send(A3, f"/{name}")
        assert h.last_text(A3) == texts.ADMIN_L2_ONLY, name
    for quick in ("@user2002 +500", "@user2002 -100 штраф"):
        await h.send(A3, quick)
        assert h.last_text(A3) == texts.ADMIN_L2_ONLY

    # /user доступен и 1 уровню — карточка без денег, бана и приоритета
    await h.send(A3, "/user @user2002")
    card = last_kb_labels(h, A3)
    assert "Борис" in h.last_text(A3) and card
    assert not {"💸 Выдать / списать ₽", "🚫 Забанить", "➕ бонусы"} & set(card)
    assert not any(x.startswith(("⭐", "🎟")) for x in card)

    # обычный пользователь: команды админов — «только для админов», кнопки — «только для админа»
    for name in ADMIN_COMMAND_NAMES:
        await h.send(A2, f"/{name} @user3003 500")
        assert h.last_text(A2) == (texts.ADMIN_ONLY if name == "admin" else texts.ADMIN_CMD_ONLY), name
    for data in (AdminCb(s="gv", a="ok", id=A2, v="50000"), AdminCb(s="g"), AdminCb(s="pt", a="pc_ok", id=1)):
        await press_data(h, A2, data)
        assert h.alerts()[-1] == texts.ADMIN_ONLY

    # ничего не изменилось
    u = await get(sessionmaker, User, A2)
    assert (u.balance_rub, u.is_banned, u.is_priority, u.free_month_until) == (0, False, False, None)
    assert config.admin_level(A2) == 0 and config.admin_level(A3) == 1
    assert await log_actions(sessionmaker, "give_money", "set_priority", "set_free_month", "ban", "db_report",
                             "backup") == 0
    assert not h.sent_documents(A3) and not h.sent_documents(A2)


# ---------------------------------------------------------------- 3. /commands и меню команд по ролям
async def test_commands_list_and_menu_follow_the_role(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"), (A4, "Глеб"), (A5, "Дина"))

    await set_commands(h.bot, config)  # при запуске: общий список + личные списки админов
    default = next(m for m in h.calls("SetMyCommands") if type(m.scope).__name__ == "BotCommandScopeDefault")
    assert [c.command for c in default.commands] == USER_NAMES
    owner_menu = next(m for m in reversed(h.calls("SetMyCommands")) if getattr(m.scope, "chat_id", None) == A1)
    assert [c.command for c in owner_menu.commands] == L2_NAMES
    assert {c.command: c.description for c in owner_menu.commands}["addadmin"] == texts.COMMANDS_OWNER_MENU["addadmin"]

    def sections(uid: int) -> list[str]:
        text = h.last_text(uid)
        return [title for title in (texts.COMMANDS_FOR_ALL, texts.COMMANDS_L1, texts.COMMANDS_L2, texts.COMMANDS_OWNER)
                if title in text]

    await h.send(A2, "/commands")
    assert sections(A2) == [texts.COMMANDS_FOR_ALL] and "/give" not in h.last_text(A2)
    await h.send(A1, "/commands")
    assert sections(A1) == [texts.COMMANDS_FOR_ALL, texts.COMMANDS_L1, texts.COMMANDS_L2, texts.COMMANDS_OWNER]
    # /help ссылается на /commands и показывает тот же список кнопкой «⌨️ Команды»
    for uid, expected in ((A2, [texts.COMMANDS_FOR_ALL]), (A1, [texts.COMMANDS_FOR_ALL, texts.COMMANDS_L1,
                                                                 texts.COMMANDS_L2, texts.COMMANDS_OWNER])):
        await h.send(uid, "/help")
        assert texts.HELP_COMMANDS_LINE in h.last_text(uid)
        await h.click(uid, texts.HELP_COMMANDS_BTN)
        assert sections(uid) == expected and texts.COMMANDS_TITLE in h.last_text(uid)

    # владелец назначает админа командой: /addadmin @username → кнопка → у него меню 1 уровня
    await h.send(A1, "/addadmin @user3003")
    assert last_kb_labels(h, A1)[0] == "🔹 Добавить: 1 уровень"
    await h.click(A1, "🔹 Добавить: 1 уровень")
    assert config.admin_level(A3) == 1 and menu_of(h, A3) == L1_NAMES
    assert h.last_text(A3) == texts.ADMIN_INVITE_OK.format(level=roles.LEVELS[1])
    await h.send(A3, "/commands")
    assert sections(A3) == [texts.COMMANDS_FOR_ALL, texts.COMMANDS_L1] and "/give" not in h.last_text(A3)

    # повысить → меню 2 уровня; /commands — с командами 2 уровня, без раздела владельца
    await h.send(A1, "/addadmin @user3003 2")
    assert h.last_text(A1).startswith("Он уже админ.")
    await h.click(A1, "⬆️ Сделать 2 уровнем")
    assert config.admin_level(A3) == 2 and menu_of(h, A3) == L2_NAMES
    await h.send(A3, "/commands")
    assert sections(A3) == [texts.COMMANDS_FOR_ALL, texts.COMMANDS_L1, texts.COMMANDS_L2]
    assert texts.COMMANDS_HINT in h.last_text(A3)

    # старший (не владелец) назначает только 1 уровень
    await h.send(A3, "/addadmin @user4004 2")
    assert h.last_text(A3) == texts.OWNER_ONLY and config.admin_level(A4) == 0
    await h.send(A3, "/addadmin 4004")
    assert "🛡 Добавить: 2 уровень" not in last_kb_labels(h, A3)
    await h.click(A3, "🔹 Добавить: 1 уровень")
    assert config.admin_level(A4) == 1 and menu_of(h, A4) == L1_NAMES
    await h.send(A3, "/addadmin @user3003")  # себя — нельзя
    assert "Он уже админ." in h.last_text(A3)

    # понизить и снять: меню обновляется, у снятого личный список удаляется
    await h.send(A1, "/admin")
    await h.click(A1, "👮 Админы")
    await h.click(A1, "🛡 Вера")
    await h.click(A1, "⬇️ Сделать 1 уровнем")
    assert config.admin_level(A3) == 1 and menu_of(h, A3) == L1_NAMES
    await h.send(A1, "/removeadmin @user3003")
    await h.click(A1, "🗑 Да, снять")
    assert config.admin_level(A3) == 0 and h.last_text(A3) == texts.ADMIN_REMOVED_NOTICE
    assert any(getattr(m.scope, "chat_id", None) == A3 for m in h.calls("DeleteMyCommands"))
    await h.send(A3, "/give @user2002 500")
    assert h.last_text(A3) == texts.ADMIN_CMD_ONLY
    await h.send(A3, "/commands")
    assert sections(A3) == [texts.COMMANDS_FOR_ALL]

    # неизвестный пользователь и неверный ввод
    await h.send(A1, "/addadmin @nobody_here")
    assert h.last_text(A1) == texts.USER_NOT_FOUND.format(query="@nobody_here")
    await h.send(A1, "/addadmin")  # без аргументов — экран «👮 Админы»
    assert "➕ Пригласить: 1 уровень" in last_kb_labels(h, A1)
    async with sessionmaker() as db:
        assert dict((await db.execute(select(Admin.user_id, Admin.level))).all()) == {A4: 1}
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- 4. деньги: выдать и списать (2 уровень)
async def test_give_and_take_money_by_username_or_id(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"))
    confirm = texts.GIVE_CONFIRM_BTN

    # /give @username 500 → подтверждение → деньги на рублёвом балансе, клиенту уведомление
    await h.send(A1, "/give @user2002 500")
    assert h.last_text(A1).startswith(f"➕ Выдать <b>{fmt_rub(50000)}</b>")
    await h.click(A1, confirm)
    assert h.last_text(A1).startswith(f"✅ Выдали {fmt_rub(50000)}") and await balance(sessionmaker, A2) == 50000
    assert h.last_text(A2) == texts.GIVE_USER_IN.format(amount=fmt_rub(50000))
    await press_data(h, A1, AdminCb(s="gv", a="ok", id=A2, v="50000"))  # повторное нажатие — не начисляем
    assert h.alerts()[-1] == texts.STALE_BUTTON and await balance(sessionmaker, A2) == 50000

    # по ID, с копейками и комментарием в той же строке — клиент видит комментарий
    await h.send(A1, "/give 2002 99,90 за решение")
    assert "Комментарий: «за решение», клиент его увидит." in h.last_text(A1)
    await h.click(A1, confirm)
    assert await balance(sessionmaker, A2) == 59990
    assert h.last_text(A2) == texts.GIVE_USER_IN.format(amount=fmt_rub(9990)) + "\n💬 за решение"

    # быстрое сообщение «@username -100» — списание
    await h.send(A1, "@user2002 -100")
    assert h.last_text(A1).startswith(f"➖ Списать <b>{fmt_rub(10000)}</b>")
    await h.click(A1, confirm)
    assert await balance(sessionmaker, A2) == 49990
    assert h.last_text(A2) == texts.GIVE_USER_OUT.format(amount=fmt_rub(10000))

    # списать больше, чем есть: предупреждение и списание до нуля, не в минус
    await h.send(A1, "@user2002 −1 000")
    assert texts.GIVE_CLAMP_WARN in h.last_text(A1)
    await h.click(A1, confirm)
    assert texts.GIVE_CLAMPED in h.last_text(A1) and await balance(sessionmaker, A2) == 0
    assert h.last_text(A2) == texts.GIVE_USER_OUT.format(amount=fmt_rub(49990))
    await h.send(A1, "@user2002 -5")  # баланс 0 — списывать нечего, сразу без подтверждения
    assert h.last_text(A1) == texts.GIVE_NOTHING and confirm not in last_kb_labels(h, A1)

    # большая сумма — второе подтверждение «проверь нули»; повтор не начисляет
    await h.send(A1, "/give @user2002 20 000")
    await h.click(A1, confirm)
    big = texts.GIVE_BIG_BTN.format(amount=fmt_rub(2000000))
    assert h.last_text(A1).startswith("⚠️ <b>Большая сумма") and big in last_kb_labels(h, A1)
    assert await balance(sessionmaker, A2) == 0
    await h.click(A1, big)
    assert await balance(sessionmaker, A2) == 2000000
    await press_data(h, A1, AdminCb(s="gv", a="big", id=A2, v="2000000"))
    assert h.alerts()[-1] == texts.STALE_BUTTON and await balance(sessionmaker, A2) == 2000000

    # переполнение 64 бит, неизвестный пользователь, непонятная сумма
    overflow = texts.GIVE_OVERFLOW.format(max=fmt_rub(INT64_MAX))
    await h.send(A1, "/give @user2002 92233720368547758,08")
    assert h.last_text(A1) == overflow
    await h.send(A1, "@user2002 +92233720368547758")  # влезает само по себе, но не вместе с балансом
    assert h.last_text(A1) == overflow
    await h.send(A1, "/give @nobody_here 100")
    assert h.last_text(A1) == texts.USER_NOT_FOUND.format(query="@nobody_here")
    await h.send(A1, "/give 777 100")
    assert h.last_text(A1) == texts.USER_NOT_FOUND.format(query="id 777")
    await h.send(A1, "/give @user2002 сто")
    assert h.last_text(A1).startswith(texts.GIVE_BAD_AMOUNT)
    await h.send(A1, "/give")
    assert h.last_text(A1) == texts.GIVE_USAGE
    assert await balance(sessionmaker, A2) == 2000000

    # кнопками: «💸 Деньги» → найти → сумма → «Без комментария» → подтвердить
    await h.send(A1, "/admin")
    await h.click(A1, "💸 Деньги")
    await h.click(A1, "🔍 Найти пользователя")
    await h.send(A1, "@nobody_here")  # не нашли — ввод открыт, можно прислать снова
    assert h.last_text(A1) == texts.USER_NOT_FOUND.format(query="@nobody_here")
    await h.send(A1, "https://t.me/user2002")
    assert h.last_text(A1).startswith("💸 <b>Деньги</b>")
    await h.send(A1, "ноль")
    assert h.last_text(A1) == texts.GIVE_BAD_AMOUNT
    await h.send(A1, "300")
    await h.click(A1, texts.GIVE_NO_COMMENT_BTN)
    await h.click(A1, confirm)
    assert await balance(sessionmaker, A2) == 2030000

    # из карточки пользователя: «💸 Выдать / списать ₽», комментарий с суммой в одной строке
    await h.send(A1, "/user @user2002")
    await h.click(A1, "💸 Выдать / списать ₽")
    await h.send(A1, "-50 ошибка в заказе")
    assert "Комментарий: «ошибка в заказе»" in h.last_text(A1)
    await h.click(A1, confirm)
    assert await balance(sessionmaker, A2) == 2025000
    assert h.last_text(A2) == texts.GIVE_USER_OUT.format(amount=fmt_rub(5000)) + "\n💬 ошибка в заказе"
    await h.send(A1, "/give @user2002 10")  # отмена — ничего не меняется
    await h.click(A1, texts.BTN_CANCEL)
    assert await balance(sessionmaker, A2) == 2025000

    # клиент не видит комментарий, если так в настройках
    await set_settings(sessionmaker, admin_give_show_comment=False)
    await h.send(A1, "/give @user2002 1 секретно")
    assert ", только в журнале." in h.last_text(A1)
    await h.click(A1, confirm)
    assert h.last_text(A2) == texts.GIVE_USER_IN.format(amount=fmt_rub(100))

    async with sessionmaker() as db:
        rows = (await db.execute(select(Transaction.kind, Transaction.amount, Transaction.admin_id)
                                 .where(Transaction.user_id == A2, Transaction.account == "rub"))).all()
        assert {k for k, _, _ in rows} == {"admin_topup", "admin_withdraw"} and {a for _, _, a in rows} == {A1}
        assert sum(a for _, a, _ in rows) == 2025100 == (await db.get(User, A2)).balance_rub
        logs = (await db.execute(select(AdminLog.details).where(AdminLog.action == "give_money"))).scalars().all()
        assert len(logs) == 8 and all('"op": "' in d for d in logs)
    await check_integrity(sessionmaker)

    # рубли от админа — обычные деньги: заказ оплачивается с баланса целиком
    oid = await wizard_order(h, A2, notes=False)
    await admin_invoice(h, oid, 20000)
    await h.click(A2, f"{texts.PAY_BTN} · {fmt_rub(20000)}")
    await h.click(A2, texts.PAY_BALANCE_BTN)
    order = await get(sessionmaker, Order, oid)
    assert (order.paid_balance, order.paid_invoice, order.paid_bonus) == (20000, 0, 0)
    assert await balance(sessionmaker, A2) == 2005100
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- 5. бан, приоритет, «Месяц бесплатно», запросы к БД
async def test_ban_priority_free_month_and_db_reports(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"))
    await set_level(sessionmaker, config, A3, 1)

    # /ban @username причина → подтверждение → бан; /unban → подтверждение → разбан
    await h.send(A1, "/ban @user2002 спамит в поддержку")
    assert h.last_text(A1) == texts.BAN_CONFIRM.format(user=BORIS, reason="спамит в поддержку")
    await h.click(A1, "🚫 Да, забанить")
    u = await get(sessionmaker, User, A2)
    assert u.is_banned and u.ban_reason == "спамит в поддержку"
    assert texts.USER_BANNED_NOTICE.format(reason=": спамит в поддержку") in h.texts(A2)
    await h.send(A1, "/unban @user2002")
    assert h.last_text(A1).startswith("✅ Разбанить")
    await h.click(A1, "✅ Да, разбанить")
    assert not (await get(sessionmaker, User, A2)).is_banned and h.last_text(A2) == texts.USER_UNBANNED

    # кнопками: «🚫 Бан / разбан» → найти → причина из списка → подтвердить; в списке — забаненные
    await h.send(A1, "/admin")
    await h.click(A1, "🚫 Бан / разбан")
    await h.click(A1, "🔍 Найти пользователя")
    await h.send(A1, "2002")
    await h.click(A1, "Спам", exact=True)
    await h.click(A1, "🚫 Да, забанить")
    assert (await get(sessionmaker, User, A2)).ban_reason == "Спам"
    await h.click(A1, "⬅️ Бан / разбан")
    await h.click(A1, "🚫 Борис")
    await h.click(A1, "✅ Разбанить")
    await h.click(A1, "✅ Да, разбанить")
    assert not (await get(sessionmaker, User, A2)).is_banned

    # админа и себя забанить нельзя — ни кнопкой, ни подделанным нажатием
    await h.send(A1, "/ban @user3003 просто так")
    assert texts.BAN_CANT_ADMIN in h.last_text(A1) and "🚫 Да, забанить" not in last_kb_labels(h, A1)
    await press_data(h, A1, AdminCb(s="bn", a="go", id=A3, v="spam"))
    assert h.alerts()[-1] == texts.BAN_CANT_ADMIN
    await press_data(h, A1, AdminCb(s="bn", a="ask", id=A1, v="spam"))
    assert h.alerts()[-1] == texts.ADMIN_CANT_SELF
    assert not (await get(sessionmaker, User, A3)).is_banned

    # «⭐ Приоритет»: команда → подтверждение → выдан; повторно — снять
    await h.send(A1, "/priority @user2002")
    assert h.last_text(A1) == texts.VIP_PRIORITY_CONFIRM_ON.format(user=BORIS)
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    assert (await get(sessionmaker, User, A2)).is_priority and h.last_text(A2) == texts.VIP_PRIORITY_ON_USER
    await h.send(A1, "/priority 2002")
    assert h.last_text(A1).startswith("Снять «Приоритет»")
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    assert not (await get(sessionmaker, User, A2)).is_priority and h.last_text(A2) == texts.VIP_PRIORITY_OFF_USER

    # «🎟 Месяц бесплатно»: дни и задачи командой, отключить — «off»
    await h.send(A1, "/freemonth @user2002 30 10")
    assert h.last_text(A1) == texts.VIP_FM_CONFIRM.format(user=BORIS, days=30, tasks=10)
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    u = await get(sessionmaker, User, A2)
    assert u.free_month_tasks_left == 10 and timedelta(days=29) < u.free_month_until - now_utc() <= timedelta(days=30)
    assert h.last_text(A2).startswith("🎟 Тебе подключён «Месяц бесплатно»")
    await h.send(A1, "/freemonth @user2002 0")
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    assert (await get(sessionmaker, User, A2)).free_month_until is None and h.last_text(A2) == texts.VIP_FM_OFF_USER
    await h.send(A1, "/freemonth @user2002 9999")
    assert h.last_text(A1).startswith("Нужно целое число от 1 до 3650")

    # кнопками: «⭐ Приоритет / 🎟 Месяц» → найти → подключить → дни из списка → своё число задач
    await h.send(A1, "/admin")
    await h.click(A1, "⭐ Приоритет / 🎟 Месяц")
    await h.click(A1, "🔍 Найти пользователя")
    await h.send(A1, "@User2002")  # регистр ника не важен
    await h.click(A1, "🎟 Подключить месяц")
    await h.click(A1, "14 дн.")
    await h.click(A1, "✏️ Своё число")
    await h.send(A1, "0")
    assert h.last_text(A1).startswith("Нужно целое число от 1 до")
    await h.send(A1, "7")
    assert h.last_text(A1) == texts.VIP_FM_CONFIRM.format(user=BORIS, days=14, tasks=7)
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    assert (await get(sessionmaker, User, A2)).free_month_tasks_left == 7
    assert "🎟 Месяц бесплатно: <b>до" in h.last_text(A1)

    # 🗄 Запросы к БД: отчёты только читают, всё пишется в журнал; файл базы — документом
    await h.send(A1, "/give @user2002 150")
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    await h.send(A1, "/db")
    assert h.last_text(A1).startswith(texts.DBQ_TITLE)
    await h.click(A1, "👤 Пользователь")
    await h.send(A1, "@user2002")
    report = h.last_text(A1)
    assert "Борис" in report and fmt_rub(15000) in report
    await h.click(A1, "⬅️ Запросы к БД")
    await h.click(A1, "💳 Платежи и начисления")
    money = h.last_text(A1)
    assert money.startswith("💳 <b>Последние платежи</b>") and f"+{fmt_rub(15000)} ₽" in money
    for label in ("📦 Последние заказы", "📋 На модерации"):
        await h.click(A1, "⬅️ Запросы к БД")
        await h.click(A1, label)
    await h.click(A1, "⬅️ Запросы к БД")
    docs = len(h.sent_documents(A1))
    await h.click(A1, "💾 Файл базы")
    await h.send(A1, "/backup")
    assert len(h.sent_documents(A1)) == docs + 2
    assert await log_actions(sessionmaker, "db_report") == 4
    await h.send(A1, "/give @user2002")  # ждём сумму — но команда из шага ввода тоже срабатывает
    assert h.last_text(A1).startswith("💸 <b>Деньги</b>")
    await h.send(A1, "/db")
    assert h.last_text(A1).startswith(texts.DBQ_TITLE)
    await h.send(A1, "500")  # ввод суммы закрыт — это уже не операция
    assert await balance(sessionmaker, A2) == 15000
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- 6. задание «📌 Ссылка в профиле»
def card_of(h, uid: int, prefix: str):  # noqa: ANN001, ANN201
    """Последняя карточка задания у админа: (сообщение, кнопки)."""
    for msg, kb in reversed(h.screen(uid)):
        if (msg.text or msg.caption or "").startswith(prefix):
            return msg, [b.text for row in (kb.inline_keyboard if kb else []) for b in row]
    raise AssertionError(f"нет карточки «{prefix}» у {uid}")


async def claims(sessionmaker) -> list[ProfileClaim]:  # noqa: ANN001
    async with sessionmaker() as db:
        return list((await db.execute(select(ProfileClaim).order_by(ProfileClaim.id))).scalars())


PROFILE_CARD = "📌 <b>Ссылка в профиле</b>"


async def test_profile_link_task_moderation_and_recheck(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"), (A4, "Глеб"))
    await set_level(sessionmaker, config, A3, 1)
    me = await get(sessionmaker, User, A2)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{me.ref_code}"
    h.fake.bios[A2] = f"Решаю задачи по начерталке: {link}"

    # «💰 Баланс»: текст про вывод, кнопки «↩️ Вернуть рубли» больше нет; оттуда — «🎁 Бонусы и акции»
    await h.send(A2, texts.MAIN_MENU["balance"])
    assert texts.WITHDRAW_SOON in h.last_text(A2)
    assert not any("Вернуть" in x for x in last_kb_labels(h, A2))
    await h.click(A2, texts.BALANCE_BONUSES_BTN)
    assert {texts.PROFILE_BTN, texts.POST_BTN, texts.MY_POSTS_BTN} <= set(last_kb_labels(h, A2))
    await h.click(A2, texts.PROFILE_BTN)
    assert f"<code>{link}</code>" in h.last_text(A2) and texts.WITHDRAW_SOON in h.last_text(A2)

    # ожидание скрина не мешает: ушёл в меню — тихо отменилось, случайное фото заявкой не станет
    await h.click(A2, texts.PROFILE_SEND_BTN)
    assert h.last_text(A2) == texts.PROFILE_SHOT_PROMPT
    await h.send(A2, texts.MAIN_MENU["balance"])
    await h.send(A2, photo="random-photo")
    assert await claims(sessionmaker) == []

    # отправка: текст вместо скрина — подсказка; фото — заявка, карточка всем админам с автопроверкой «О себе»
    await press_data(h, A2, PromoCb(action="profile_send"))
    await h.send(A2, "вот, добавил")
    assert h.last_text(A2) == texts.PROMO_WAIT_FILE
    await h.send(A2, photo="profile-1")
    assert h.last_text(A2) == texts.PROFILE_SENT.format(reward=fmt_rub(10000))
    for admin in (A1, A3):
        card, buttons = card_of(h, admin, PROFILE_CARD)
        assert texts.BIO_CHECK["found"] in card.caption and f"<code>{link}</code>" in card.caption
        assert buttons[:2] == [texts.TASK_PAY_BTN, texts.TASK_REJECT_BTN]
    await h.send(A1, "/admin")
    assert texts.TASKS_ATTENTION.format(n="<b>1</b>") in h.last_text(A1)

    # пока заявка на проверке — новую не отправить
    await press_data(h, A2, PromoCb(action="profile"))
    assert texts.PROFILE_SEND_BTN not in last_kb_labels(h, A2) and "⏳ Заявка уже на проверке" in h.last_text(A2)
    await press_data(h, A2, PromoCb(action="profile_send"))
    assert h.alerts()[-1].startswith("⏳ Заявка уже на проверке")

    # 1 уровень отклоняет с причиной → пользователю причина, у другого админа карточка помечена
    await h.click(A3, texts.TASK_REJECT_BTN)
    await h.click(A3, texts.TASK_REJECT_REASONS["nolink"].capitalize())
    assert h.last_text(A2) == texts.PROFILE_REJECTED_USER.format(reason=": не видно ссылки на бота")
    card, buttons = card_of(h, A1, PROFILE_CARD)
    assert "❌ Отклонено: не видно ссылки на бота: Вера" in card.text and texts.TASK_PAY_BTN not in buttons
    await press_data(h, A1, AdminCb(s="pt", a="pc_ok", id=1))  # старая кнопка — уже решено
    assert h.alerts()[-1].startswith("По этой заявке уже есть решение") and await balance(sessionmaker, A2) == 0

    # после отказа можно снова; одобрение 2 уровнем → +100 ₽ на рубли, повтор не начисляет
    await h.click(A2, texts.PROFILE_BTN)
    assert "❌ Прошлую заявку мы отклонили: не видно ссылки на бота" in h.last_text(A2)
    await h.click(A2, texts.PROFILE_SEND_BTN)
    await h.send(A2, photo="profile-2")
    await h.click(A1, texts.TASK_PAY_BTN)
    assert await balance(sessionmaker, A2) == 10000
    assert h.last_text(A2) == texts.PROFILE_APPROVED_USER.format(amount=fmt_rub(10000))
    card, buttons = card_of(h, A3, PROFILE_CARD)
    assert f"✅ Начислено {fmt_rub(10000)}: Аня" in card.text and texts.TASK_PAY_BTN not in buttons
    await press_data(h, A3, AdminCb(s="pt", a="pc_ok", id=2))
    assert h.alerts()[-1] == "По этой заявке уже есть решение: ✅ засчитана."
    assert await balance(sessionmaker, A2) == 10000
    await press_data(h, A2, PromoCb(action="profile"))
    assert "✅ Задание выполнено" in h.last_text(A2) and texts.PROFILE_SEND_BTN not in last_kb_labels(h, A2)

    # свою заявку админ 1 уровня не одобряет; бан — без награды; Telegram не ответил — «не удалось проверить»
    h.fake.bios[A3] = f"https://t.me/{BOT_USERNAME}?start=ref_{(await get(sessionmaker, User, A3)).ref_code}"
    await press_data(h, A3, PromoCb(action="profile_send"))
    await h.send(A3, photo="profile-3")
    await h.click(A3, texts.TASK_PAY_BTN)
    assert h.alerts()[-1] == texts.ADMIN_CANT_SELF and await balance(sessionmaker, A3) == 0
    await h.click(A1, texts.TASK_PAY_BTN)
    assert await balance(sessionmaker, A3) == 10000

    h.fake.getchat_error = "Bad Request: chat not found"
    await press_data(h, A4, PromoCb(action="profile_send"))
    await h.send(A4, photo="profile-4")
    h.fake.getchat_error = None
    card, _ = card_of(h, A1, PROFILE_CARD)
    assert texts.BIO_CHECK["unknown"] in card.caption
    await h.send(A1, "/ban @user4004 накрутка")
    await h.click(A1, "🚫 Да, забанить")
    await press_data(h, A1, AdminCb(s="pt", a="pc_ok", id=4))
    assert h.alerts()[-1] == plain(texts.TASK_BANNED) and await balance(sessionmaker, A4) == 0
    await press_data(h, A1, AdminCb(s="pt", a="r", id=4, v="pc.fake"))
    assert [c.status for c in await claims(sessionmaker)] == ["rejected", "approved", "approved", "rejected"]

    # перепроверка через N дней: ссылки в «О себе» нет — награда списывается (не ниже нуля), уведомление
    await set_settings(sessionmaker, profile_recheck_days=7)
    assert await scheduler.profile_recheck(h.bot, sessionmaker, now=now_utc() + timedelta(days=3)) == 0
    h.fake.bios[A2] = "теперь тут ничего"
    assert await scheduler.profile_recheck(h.bot, sessionmaker, now=now_utc() + timedelta(days=8)) == 1
    assert await balance(sessionmaker, A2) == 0 and await balance(sessionmaker, A3) == 10000
    assert h.last_text(A2) == texts.PROFILE_REVOKED_USER.format(amount=fmt_rub(10000))
    statuses = [(c.user_id, c.status) for c in await claims(sessionmaker)]
    assert statuses == [(A2, "rejected"), (A2, "revoked"), (A3, "approved"), (A4, "rejected")]
    assert await scheduler.profile_recheck(h.bot, sessionmaker, now=now_utc() + timedelta(days=30)) == 0
    await press_data(h, A2, PromoCb(action="profile"))
    assert "Награду за ссылку в профиле мы списали" in h.last_text(A2)
    assert texts.PROFILE_SEND_BTN not in last_kb_labels(h, A2)
    async with sessionmaker() as db:
        kinds = (await db.execute(select(Transaction.kind, Transaction.amount).where(Transaction.user_id == A2)
                                  .order_by(Transaction.id))).all()
        assert kinds == [("profile_link", 10000), ("profile_link_revoke", -10000)]
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- 7. задание «📣 Пост о нас» и отчёты о просмотрах
POST_CARD, VIEWS_CARD = "📣 <b>Пост о нас</b>", "📈 <b>Просмотры</b>"
TG_URL = "https://t.me/somechannel/123"


async def report_views(h, uid: int, pid: int, views: str) -> None:  # noqa: ANN001
    await press_data(h, uid, PromoCb(action="posts"))
    await h.click(uid, f"{texts.VIEWS_BTN} · №{pid}")
    await h.send(uid, photo=f"views-{uid}-{pid}-{views}")
    await h.send(uid, views)


async def test_post_task_reward_views_and_duplicates(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"), (A4, "Глеб"))
    await set_level(sessionmaker, config, A3, 1)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{(await get(sessionmaker, User, A2)).ref_code}"

    # экран задания: шаблон поста с реф-ссылкой, текст про вывод; TG → ссылка → скрин
    await press_data(h, A2, MenuCb(action="h_bonuses"))
    await h.click(A2, texts.POST_BTN)
    assert f"<pre>{texts.DEFAULT_POST_TEMPLATE.replace('{link}', link)}</pre>" in h.last_text(A2)
    assert texts.WITHDRAW_SOON in h.last_text(A2)
    await h.click(A2, texts.POST_TG_BTN)
    assert h.last_text(A2) == texts.POST_URL_PROMPT["tg"]
    await h.send(A2, "мой канал")
    assert h.last_text(A2) == texts.POST_URL_BAD["tg"]
    await h.send(A2, "t.me/SomeChannel/123?single")  # ссылку приводим к одному виду
    assert h.last_text(A2) == texts.POST_SHOT_PROMPT
    await h.send(A2, photo="post-1")
    assert h.last_text(A2) == texts.POST_SENT.format(base=fmt_rub(20000))
    card, buttons = card_of(h, A3, POST_CARD)
    assert TG_URL in card.caption and buttons[:2] == [texts.TASK_ACCEPT_BTN, texts.TASK_REJECT_BTN]
    pid = (await get(sessionmaker, PromoPost, 1)).id
    await h.send(A1, "/admin")
    assert texts.TASKS_ATTENTION.format(n="<b>1</b>") in h.last_text(A1)

    # тот же пост с другого аккаунта — отказ сразу; свой повтор — «уже у нас»
    await press_data(h, A4, PromoCb(action="plat", v="tg"))
    await h.send(A4, "https://telegram.me/somechannel/123")
    assert h.last_text(A4) == texts.POST_URL_DUP_OTHER
    await press_data(h, A2, PromoCb(action="plat", v="tg"))
    await h.send(A2, TG_URL)
    assert h.last_text(A2) == texts.POST_URL_DUP_OWN.format(status=texts.POST_STATUS["pending"])
    await h.click(A2, texts.BTN_CANCEL)
    assert h.last_text(A2) == texts.PROMO_CANCELLED

    # 1 уровень принимает пост → +200 ₽ на рубли, у другого админа карточка помечена
    await h.click(A3, texts.TASK_ACCEPT_BTN)
    assert await balance(sessionmaker, A2) == 20000
    assert h.last_text(A2).startswith(f"🎉 Пост принят: <b>+{fmt_rub(20000)}</b>")
    card, buttons = card_of(h, A1, POST_CARD)
    assert f"✅ Пост принят, +{fmt_rub(20000)}: Вера" in card.text and texts.TASK_ACCEPT_BTN not in buttons
    await press_data(h, A1, AdminCb(s="pt", a="rp_ok", id=1))
    assert h.alerts()[-1].startswith("По этой заявке уже есть решение") and await balance(sessionmaker, A2) == 20000

    # «📣 Мои посты»: статус и кнопка просмотров; отчёт не чаще раза в 12 ч (по умолчанию)
    await h.click(A2, texts.MY_POSTS_BTN)
    assert f"📣 №{pid} · Telegram · {TG_URL}" in h.last_text(A2) and "✅ принят" in h.last_text(A2)
    await h.click(A2, f"{texts.VIEWS_BTN} · №{pid}")
    assert h.alerts()[-1].startswith("⏳ Отчёт по этому посту можно присылать не чаще раза в 12 ч")
    await set_settings(sessionmaker, post_report_cooldown_hours=0)

    # отчёт: скрин + число; пока на проверке — второй нельзя; 150 просмотров → +100 ₽
    await h.click(A2, f"{texts.VIEWS_BTN} · №{pid}")
    assert h.last_text(A2) == texts.VIEWS_SHOT_PROMPT.format(views=0)
    await h.send(A2, photo="views-1")
    assert h.last_text(A2) == texts.VIEWS_COUNT_PROMPT
    await h.send(A2, "сто пятьдесят")
    assert h.last_text(A2) == texts.VIEWS_COUNT_BAD
    await h.send(A2, "150")
    assert h.last_text(A2) == texts.VIEWS_SENT.format(n=150)
    await press_data(h, A2, PromoCb(action="views", id=pid))
    assert h.alerts()[-1] == plain(texts.VIEWS_PENDING)
    card, buttons = card_of(h, A1, VIEWS_CARD)
    assert f"Если подтвердить 150: к выплате {fmt_rub(10000)}" in card.caption
    await h.click(A1, texts.TASK_VIEWS_OK_BTN.format(n=150))
    assert await balance(sessionmaker, A2) == 30000
    assert h.last_text(A2) == texts.VIEWS_APPROVED_USER.format(views=150, amount=fmt_rub(10000), total=fmt_rub(30000))

    # 1000 просмотров: админ вводит своё число — меньше прошлого нельзя; доплата = всего − уже выплачено
    await report_views(h, A2, pid, "1000")
    await h.click(A3, texts.TASK_VIEWS_NUM_BTN)
    assert h.last_text(A3) == texts.TASK_VIEWS_ASK.format(prev=150)
    await h.send(A3, "50")
    assert h.last_text(A3) == texts.TASK_VIEWS_BAD.format(prev=150)
    await h.send(A3, "1000")
    assert await balance(sessionmaker, A2) == 120000
    assert h.last_text(A2) == texts.VIEWS_APPROVED_USER.format(views=1000, amount=fmt_rub(90000),
                                                               total=fmt_rub(120000))

    # пользователь не может заявить меньше подтверждённого; до следующей сотни — доплаты нет
    await report_views(h, A2, pid, "900")
    assert h.last_text(A2) == texts.VIEWS_NOT_MORE.format(n=1000)
    await h.send(A2, "1050")
    await h.click(A1, texts.TASK_VIEWS_OK_BTN.format(n=1050))
    assert await balance(sessionmaker, A2) == 120000
    assert h.last_text(A2) == texts.VIEWS_APPROVED_ZERO_USER.format(views=1050, next=1100, total=fmt_rub(120000))

    # отказ по отчёту со своей причиной
    await report_views(h, A2, pid, "5000")
    await h.click(A1, texts.TASK_REJECT_BTN)
    await h.click(A1, texts.TASK_CUSTOM_REASON_BTN)
    await h.send(A1, "на скрине другой пост")
    assert h.last_text(A2) == texts.VIEWS_REJECTED_USER.format(url=TG_URL, reason=": на скрине другой пост")
    assert await balance(sessionmaker, A2) == 120000

    # ожидание скрина тихо отменяется, если нажать кнопку другого раздела
    await press_data(h, A2, PromoCb(action="views", id=pid))
    assert h.last_text(A2).startswith("📈 Пришли скрин")
    await press_data(h, A2, MenuCb(action="history"))
    await h.send(A2, photo="stray-photo")
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count(PromoPostReport.id))) == 5

    # пост ВКонтакте: отказ с причиной из списка, потом тот же свой пост можно прислать снова
    await press_data(h, A2, PromoCb(action="plat", v="vk"))
    await h.send(A2, "https://m.vk.com/wall-123456_789?from=group")
    await h.send(A2, photo="vk-1")
    await h.click(A1, texts.TASK_REJECT_BTN)
    await h.click(A1, texts.TASK_REJECT_REASONS["foreign"].capitalize())
    assert h.last_text(A2) == texts.POST_REJECTED_USER.format(url="https://vk.com/wall-123456_789",
                                                              reason=": чужой пост или профиль")
    await press_data(h, A2, PromoCb(action="plat", v="vk"))
    await h.send(A2, "vk.com/wall-123456_789")
    await h.send(A2, photo="vk-2")
    vk = await get(sessionmaker, PromoPost, 2)
    assert (vk.platform, vk.status, vk.post_url) == ("vk", "pending", "https://vk.com/wall-123456_789")

    # активных постов не больше настройки
    await set_settings(sessionmaker, post_max_active=2)
    await press_data(h, A2, PromoCb(action="post"))
    assert texts.POST_LIMIT.format(n=2) in h.last_text(A2) and texts.POST_TG_BTN not in last_kb_labels(h, A2)
    await press_data(h, A2, PromoCb(action="plat", v="tg"))
    assert h.alerts()[-1] == plain(texts.POST_LIMIT.format(n=2))

    # ручная корректировка ₽ по посту — только 2 уровень
    await h.click_data(A3, AdminCb(s="pt", a="post", id=pid).pack())
    assert texts.TASK_ADJUST_BTN not in last_kb_labels(h, A3)
    await press_data(h, A3, AdminCb(s="pt", a="adj", id=pid))
    assert h.alerts()[-1] == texts.ADMIN_L2_ONLY
    await h.click_data(A1, AdminCb(s="pt", a="post", id=pid).pack())
    await h.click(A1, texts.TASK_ADJUST_BTN)
    await h.send(A1, "-50 штраф за накрутку")
    assert h.last_text(A1).startswith(f"✏️ Корректировка по посту №{pid}: <b>−{fmt_rub(5000)}</b>")
    await h.click(A1, texts.GIVE_CONFIRM_BTN)
    assert await balance(sessionmaker, A2) == 115000
    assert h.last_text(A2) == texts.POST_ADJUST_USER.format(url=TG_URL, amount=f"−{fmt_rub(5000)}",
                                                            comment="\n💬 штраф за накрутку")
    await press_data(h, A1, AdminCb(s="pt", a="adj_go", id=pid))  # повторное нажатие
    assert h.alerts()[-1] == texts.STALE_BUTTON and await balance(sessionmaker, A2) == 115000

    # «🗑 Пост удалён»: новые отчёты не принимаем, начисленное остаётся
    await h.click(A3, texts.TASK_DELETED_BTN)
    await h.click(A3, texts.TASK_DELETED_GO_BTN)
    assert (await get(sessionmaker, PromoPost, pid)).status == "deleted"
    assert h.last_text(A2) == texts.POST_DELETED_USER.format(url=TG_URL)
    await press_data(h, A2, PromoCb(action="views", id=pid))
    assert h.alerts()[-1] == plain(texts.VIEWS_CLOSED.format(status=texts.POST_STATUS["deleted"]))
    await press_data(h, A2, PromoCb(action="posts"))
    assert f"{texts.VIEWS_BTN} · №{pid}" not in last_kb_labels(h, A2)
    assert await balance(sessionmaker, A2) == 115000

    post = await get(sessionmaker, PromoPost, pid)
    assert (post.base_reward_paid, post.views_reward_paid, post.adjust_total, post.verified_views) == \
        (20000, 100000, -5000, 1050)
    async with sessionmaker() as db:
        kinds = (await db.execute(select(Transaction.kind, Transaction.amount).where(Transaction.user_id == A2)
                                  .order_by(Transaction.id))).all()
        assert kinds == [("promo_post", 20000), ("promo_views", 10000), ("promo_views", 90000),
                         ("promo_adjust", -5000)]
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- 8. дымовой обход новых экранов с заявками на проверке
async def test_admin_screens_smoke_with_pending_tasks(e2e, config, sessionmaker) -> None:
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"))
    await set_level(sessionmaker, config, A3, 1)
    await set_settings(sessionmaker, post_report_cooldown_hours=0)
    await press_data(h, A2, PromoCb(action="profile_send"))
    await h.send(A2, photo="profile-1")
    await press_data(h, A2, PromoCb(action="plat", v="tg"))
    await h.send(A2, TG_URL)
    await h.send(A2, photo="post-1")
    await h.click(A1, texts.TASK_ACCEPT_BTN)
    await report_views(h, A2, 1, "300")
    await press_data(h, A2, PromoCb(action="plat", v="vk"))
    await h.send(A2, "https://vk.com/wall-1_2")
    await h.send(A2, photo="vk-1")
    async with sessionmaker() as db:
        assert (await db.scalar(select(func.count(PromoPostReport.id)).where(PromoPostReport.status == "pending"))) == 2

    pressed = await h.crawl(A3, entry="/admin")
    for label in (texts.TASKS_BTN, texts.TASKS_ALL_POSTS_BTN, texts.TASK_SHOT_BTN, texts.TASK_POST_BTN.format(id=1)):
        assert any(p.startswith(label) for p in pressed), label
    l2_only = {texts.MANAGE_BTN, "👮 Админы", "⭐ Приоритет / 🎟 Месяц", "🚫 Бан / разбан", "💸 Деньги",
               "🗄 Запросы к БД", "💸 Выдать / списать ₽", texts.TASK_ADJUST_BTN}
    assert not l2_only & set(pressed)

    pressed = await h.crawl(A1, entry="/admin", limit=2000)
    assert len(pressed) > 300, len(pressed)
    assert texts.MANAGE_BTN in pressed or "⬅️ Управление" in pressed  # одна и та же кнопка-раздел
    for label in ("⭐ Приоритет / 🎟 Месяц", "🚫 Бан / разбан", "💸 Деньги", "🗄 Запросы к БД",
                  "📋 На модерации", "💳 Платежи и начисления", "🎯 Задания: профиль и посты", "💸 Деньги от админов"):
        assert any(p.startswith(label) for p in pressed), label
    await check_integrity(sessionmaker)


# ---------------------------------------------------------------- суммы денег у предела 64 бит
async def test_money_totals_survive_balances_near_int64_limit(e2e, config, sessionmaker) -> None:
    """Баланс одного человека может дойти до предела BIGINT, а сумма по всем — уже нет: SUM() в SQLite падает
    с «integer overflow», поэтому статистика, проверка целостности и отчёты считают суммы в Python."""
    h, _ = e2e
    await start(h, (A1, "Аня"), (A2, "Борис"), (A3, "Вера"))
    big = INT64_MAX - 100
    async with sessionmaker() as db:
        for uid in (A2, A3):
            res = await admin_money.give(db, uid, big, "тест предела", A1, f"limit-{uid}", now_utc())
            assert res.after == big
    async with sessionmaker() as db:
        with pytest.raises(OperationalError, match="integer overflow"):  # так было бы через SQL
            await db.scalar(select(func.sum(User.balance_rub)))
    async with sessionmaker() as db:
        assert await integrity_issues(db) == []
        assert (await ledger_totals(db))[1] == 2 * big > INT64_MAX
        assert fmt_rub(2 * big) in await stats.admin_report(db, now_utc())
    await h.send(A1, "/admin")
    await h.click(A1, "📊 Статистика")
    assert fmt_rub(2 * big) in h.last_text(A1)
    await h.send(A1, "/user @user2002")
    assert fmt_rub(big) in h.last_text(A1)
