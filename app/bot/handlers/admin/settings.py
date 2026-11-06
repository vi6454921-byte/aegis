"""Админка → настройки: цены, срочность, скидки, лимиты, рефералка, оплата (режим - с подтверждением),
тексты, ссылки на подарки."""
from __future__ import annotations

import html
from decimal import Decimal

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import guide
from app.bot.callbacks import AdminCb
from app.bot.handlers.admin.base import admin_router, ask, ask_new, cancel_btn, close_prompt, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.states import AdminSG
from app.bot.ui import show
from app.db.models import Payment
from app.services import channel as ch
from app.services import gifts, maintenance, paymode
from app.services.admin_log import log_admin
from app.services.settings import (
    ENUM_LABELS,
    GROUPS,
    META,
    REAL_PAYMENT_MODES,
    RuntimeSettings,
    cycle_enum,
    default_value,
    format_value,
    is_texty,
    load_settings,
    parse_value,
    set_setting,
    setting_type,
)
from app.texts import PAY_TEST_BTN, WELCOME_PLACEHOLDERS
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc

router = admin_router("admin_settings", level=2)
HINTS = {
    "money": "Сумма в рублях, например <code>200</code> или <code>99,90</code>.",
    "int": "Целое число.",
    "pct": "Процент от 0 до 100.",
    "mult": "Множитель, например <code>1,5</code>.",
    "decimal": "Число, например <code>1,75</code>.",
    "money_list": "Суммы через запятую, например <code>200, 500, 1000</code>.",
    "money_map": "Пакет=бонус через запятую, например <code>500=50, 1000=150</code>.",
    "text": "Пришли новый текст. Жирный, курсив и ссылки сохранятся.",
    "template": "Пришли новый текст поста. Обязательно оставь <code>{link}</code>: туда подставится реф-ссылка "
                "пользователя. Пользователь увидит текст в рамке, откуда его удобно скопировать.",
    "str": "Публичный канал: <code>@username</code> или ссылка <code>t.me/username</code>. "
           "<code>-</code>: выключить канал и бонусы за подписку.",
    "url": "Пришли ссылку, например <code>https://username.github.io/page/</code>. "
           "<code>-</code>: скрыть этот подарок.",
}
KEY_HINTS = {
    "urgency_free_hours": "Например, <code>48</code>: дедлайн через 48 ч и позже, без наценки.",
    "urgency_mid_hours": "Например, <code>24</code>: дедлайн меньше чем через 24 ч, максимальная наценка.",
    "urgency_ge72_pct": "Обычно <code>1</code>: без наценки.",
    "channel_hold_hours": "Если отписаться раньше: бонус спишется. <code>0</code>, без удержания.",
    "referral_bonus": "<code>0</code>: выключить (основной бонус, за подписку друга на канал).",
    "pay_reminder_hours": "<code>0</code>: не напоминать.",
    "profile_recheck_days": "Через сколько дней после начисления проверить, что ссылка ещё в профиле. "
                            "<code>0</code>: не перепроверять.",
    "post_views_step": "Например, <code>100</code>: за каждые 100 подтверждённых просмотров, сумма из "
                       "следующей настройки.",
    "post_views_cap": "<code>0</code>: без потолка.",
    "post_report_cooldown_hours": "<code>0</code>: без паузы между отчётами.",
    "admin_give_confirm_over": "Например, <code>10000</code>: выдача или списание больше 10 000 ₽, с повторным "
                               "подтверждением «проверь нули».",
}


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "s") & (F.a == action))


def group_of(key: str) -> str:
    return next((g for g, (_, keys) in GROUPS.items() if key in keys), "prices")


def groups_view() -> tuple[str, InlineKeyboardMarkup]:
    rows = [[cb(title, AdminCb(s="s", a="grp", v=g))] for g, (title, _) in GROUPS.items()]
    rows.append([menu_btn()])
    return "⚙️ <b>Настройки</b>\nИзменения действуют сразу, без перезапуска.", ik(*rows)


def group_extra(g: str, s: RuntimeSettings) -> list[str]:
    """Пояснение под настройками раздела: как это увидят клиенты, что ещё нужно сделать."""
    if g == "urgency":
        return ["", "<b>Так видят клиенты:</b>", *guide.urgency_lines(s),
                "<i>Тексты в боте, подсказки на кнопках сроков и формула цены пересчитываются сами.</i>"]
    if g == "channel":
        lines = [""]
        if not s.channel:
            lines.append("📢 Канал выключен: бонусов за подписку нет.")
        else:
            lines.append(f"📢 Бот должен быть <b>администратором</b> {html.escape(s.channel)} (минимальных прав "
                         "достаточно): иначе Telegram не даёт проверить подписку.")
            if ch.health.error:
                lines.append(f"⚠️ Последняя проверка не удалась: <i>{html.escape(ch.health.error[:200])}</i>")
            elif ch.health.ok_at:
                lines.append("✅ Проверка подписки работает.")
        lines.append("<i>Защита от накрутки: бонус за подписку, один раз на аккаунт; друг засчитывается, только "
                     "если впервые пришёл по ссылке; пригласивший тоже должен быть подписан; отписка в течение "
                     "удержания отменяет бонус; есть лимит друзей в сутки; забаненным не начисляется.</i>")
        return lines
    if g == "tasks":
        lines = guide.task_lines(s)
        return ["", "<b>Так видят клиенты</b> (в «🎁 Бонусы и акции»):", *(lines or ["• оба задания выключены"]),
                "", "<i>Награды: обычные рубли на баланс, они не сгорают, ими можно оплатить заказ целиком. "
                    "Заявки и отчёты проверяют админы 1 и 2 уровня в «🎯 Задания».</i>"]
    if g == "admin_money":
        return ["", "<i>Выдать или списать деньги: «🔐 Управление» → «💸 Деньги», карточка пользователя, команда "
                    "<code>/give @username 500</code> или сообщение <code>@username +500</code>. Это обычные рубли "
                    "на балансе; списание не уводит баланс ниже нуля.</i>"]
    if g == "gifts":
        block = gifts.welcome_block(s)
        return ["", "<b>Так видят клиенты</b> (в приветствии и в «🎁 Подарки»):", block or "• все подарки скрыты",
                "", "<i>Подарки: только ссылки в тексте сообщения, без кнопок и файлов. Новая ссылка действует "
                    "сразу. Названия подарков: в app/services/gifts.py.</i>"]
    return []


def group_view(g: str, s: RuntimeSettings) -> tuple[str, InlineKeyboardMarkup]:
    title, keys = GROUPS[g]
    lines = [f"⚙️ <b>{title}</b>"]
    rows = []
    for key in keys:
        label = META[key][1]
        value = format_value(key, s[key])
        if is_texty(key):
            lines.append(f"• {label}: <i>{html.escape(value)}</i>")
        else:
            lines.append(f"• {label}: <b>{html.escape(value)}</b>")
        data = AdminCb(s="s", a="pm") if key == "payments_mode" else AdminCb(s="s", a="key", v=key)
        rows.append([cb(f"{label}: {value}"[:60], data)])
    lines += group_extra(g, s)
    instant = any(setting_type(k) == "bool" or (setting_type(k).startswith("enum:") and k != "payments_mode")
                  for k in keys)
    lines.append("\nНажми на параметр, чтобы изменить." + (" Вкл/выкл и режимы переключаются сразу." if instant
                                                           else ""))
    rows.append([cb("⬅️ Назад", AdminCb(s="s")), menu_btn()])
    return "\n".join(lines)[:4000], ik(*rows)


def back_to(key: str) -> AdminCb:
    return AdminCb(s="s", a="grp", v=group_of(key))


async def _save(session: AsyncSession, admin_id: int, key: str, old, new) -> RuntimeSettings:  # noqa: ANN001
    await set_setting(session, key, new)
    log_admin(session, admin_id, "set_setting", "setting", key, old=old, new=new)
    await session.commit()
    return await load_settings(session)


@router.callback_query(_f(""))
async def s_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, *groups_view())


@router.callback_query(_f("grp"))
async def s_group(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, s: RuntimeSettings) -> None:
    if callback_data.v not in GROUPS:
        await call.answer("Нет такого раздела")
        return
    await state.clear()
    await call.answer()
    await show(call, *group_view(callback_data.v, s))


@router.callback_query(_f("key"))
async def s_key(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                s: RuntimeSettings) -> None:
    key = callback_data.v
    if key not in META:
        await call.answer("Нет такой настройки")
        return
    kind = setting_type(key)
    if key == "payments_mode":  # не по кругу: отдельный экран с подтверждением
        await state.clear()
        await call.answer()
        await show(call, *await pm_view(session, s))
        return
    if kind == "bool" or kind.startswith("enum:"):
        new = (not s[key]) if kind == "bool" else cycle_enum(key, s[key])
        s2 = await _save(session, call.from_user.id, key, s[key], new)
        await call.answer(f"{META[key][1]}: {format_value(key, new)}")
        await show(call, *group_view(group_of(key), s2))
        return
    await call.answer()
    label = META[key][1]
    kb = ik([cb(f"↩️ По умолчанию: {format_value(key, default_value(key))}"[:60], AdminCb(s="s", a="reset", v=key))],
            [cancel_btn(back_to(key))])
    if kind == "text":  # текущий текст - отдельным сообщением, чтобы его было удобно скопировать и поправить
        await call.message.answer(f"✏️ <b>{label}</b>: сейчас так 👇")
        await call.message.answer(s[key] or "<i>(пусто)</i>")
        text = HINTS["text"]
        if key == "welcome_text":
            text += f"\nПодстановки: {html.escape(WELCOME_PLACEHOLDERS)}"
        if key == "manual_pay_details":
            text += "\n<code>-</code>: очистить."
        await ask_new(call.message, state, AdminSG.setting_value, text, kb, key=key)
        return
    if kind == "template":  # шаблон - простой текст: показываем как есть, в рамке (удобно скопировать)
        await call.message.answer(f"✏️ <b>{label}</b>: сейчас так 👇")
        await call.message.answer(f"<pre>{html.escape(str(s[key]))}</pre>")
        await ask_new(call.message, state, AdminSG.setting_value, HINTS["template"], kb, key=key)
        return
    hint = " ".join(h for h in (HINTS.get(kind, ""), KEY_HINTS.get(key, "")) if h)
    text = f"✏️ <b>{label}</b>\nСейчас: <b>{html.escape(format_value(key, s[key]))}</b>\n{hint}\n\nПришли новое значение 👇"
    await ask(call, state, AdminSG.setting_value, text, kb, key=key)


@router.message(AdminSG.setting_value)
async def s_value(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings) -> None:
    key = (await state.get_data()).get("key")
    if key not in META:
        await state.clear()
        return
    kind = setting_type(key)
    raw = (message.html_text if kind == "text" else message.text) or ""
    if kind == "url":  # ссылка, спрятанная в тексте («Добавить ссылку» в Telegram), - берём сам адрес
        raw = next((e.url for e in (message.entities or []) if e.type == "text_link" and e.url), raw)
    try:
        value = parse_value_checked(key, raw, s)
    except ValueError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}. Пришли другое значение или нажми «Отмена».",
                             reply_markup=ik([cancel_btn(back_to(key))]))
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(message.bot, data)
    s2 = await _save(session, message.from_user.id, key, s[key], value)
    text, kb = group_view(group_of(key), s2)
    shown = "текст обновлён" if is_texty(key) else html.escape(format_value(key, value))
    await message.answer(f"✅ {META[key][1]}: {shown}\n\n{text}"[:4096], reply_markup=kb)


def parse_value_checked(key: str, raw: str, s: RuntimeSettings):  # noqa: ANN201
    value = parse_value(key, raw)
    if key == "stats_offset_first_try" and int(value) > int(s.stats_offset_done):
        raise ValueError("Не может быть больше стартового числа сданных работ")
    if key == "stats_offset_done" and int(value) < int(s.stats_offset_first_try):
        raise ValueError("Не может быть меньше числа «с первой попытки»")
    if key == "rub_per_star" and Decimal(value) > Decimal("1000"):
        raise ValueError("Слишком большой курс")
    if key == "urgency_free_hours" and int(value) <= int(s.urgency_mid_hours):
        raise ValueError(f"Должно быть больше порога максимальной наценки ({s.urgency_mid_hours} ч)")
    if key == "urgency_mid_hours" and int(value) >= int(s.urgency_free_hours):
        raise ValueError(f"Должно быть меньше порога «без наценки» ({s.urgency_free_hours} ч)")
    return value


@router.callback_query(_f("reset"))
async def s_reset(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                  s: RuntimeSettings) -> None:
    key = callback_data.v
    if key not in META:
        await call.answer("Нет такой настройки")
        return
    if key in ("urgency_free_hours", "urgency_mid_hours"):
        try:
            parse_value_checked(key, str(default_value(key)), s)
        except ValueError as e:
            await call.answer(f"Нельзя: {e}"[:200], show_alert=True)
            return
    data = await state.get_data()
    await state.clear()
    s2 = await _save(session, call.from_user.id, key, s[key], default_value(key))
    await call.answer("Сброшено к значению по умолчанию")
    text, kb = group_view(group_of(key), s2)
    if data.get("prompt_mid") == getattr(call.message, "message_id", None):
        await show(call, text, kb)  # вопрос был на месте экрана - возвращаем экран туда же
    else:
        await close_prompt(call.bot, data)
        await call.message.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ режим оплаты
MODE_ON = {"stars": "⭐ Включить оплату звёздами", "manual": "🏦 Включить оплату переводом",
           "test": "🧪 Вернуть тестовую оплату"}
MODE_TO = {"stars": "⭐ Перейти на звёзды", "manual": "🏦 Перейти на перевод"}
MODE_DONE = {"stars": "✅ Включена оплата звёздами (Telegram Stars).",
             "manual": "✅ Включена оплата переводом с проверкой чека.",
             "test": "🧪 Включена тестовая оплата."}


def mode_label(mode: str) -> str:
    return ENUM_LABELS["payments_mode"].get(mode, mode)


def _rate(s: RuntimeSettings) -> str:
    return f"1 ⭐ = {str(s.rate).replace('.', ',')} ₽"


async def _real_pending(session: AsyncSession) -> int:
    """Выставленные, но не оплаченные счета звёздами/переводом - при возврате теста они остаются в силе."""
    return int(await session.scalar(select(func.count(Payment.id)).where(
        Payment.status == "pending", Payment.provider.in_(REAL_PAYMENT_MODES))) or 0)


def _no_details(s: RuntimeSettings) -> bool:
    return not str(s.manual_pay_details or "").strip()


async def pm_view(session: AsyncSession, s: RuntimeSettings, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    mode = s.payments_mode
    lines = [note, ""] if note else []
    lines += ["💳 <b>Режим оплаты</b>", f"Сейчас: <b>{mode_label(mode)}</b>"]
    rows = []
    if mode == "test":
        lines.append(f"Деньги воображаемые: под счётом кнопка «{PAY_TEST_BTN}», ничего не списывается.")
        lines += ["", "При включении настоящей оплаты тестовая исчезает полностью: кнопок теста не останется, "
                      "тестовые счета и деньги уберутся. Вернуть тест можно здесь же."]
        rows += [[cb(MODE_ON["stars"], AdminCb(s="s", a="pm_ask", v="stars"))],
                 [cb(MODE_ON["manual"], AdminCb(s="s", a="pm_ask", v="manual"))]]
    else:
        if mode == "stars":
            lines.append(f"Клиенты платят звёздами прямо в Telegram. Курс: {_rate(s)}, меняется в «💳 Оплата».")
        else:
            lines.append("Клиент переводит по реквизитам и присылает чек, админ подтверждает «✅ Оплата пришла».")
            if _no_details(s):
                lines.append("⚠️ Реквизиты не указаны: впиши их в «💳 Оплата» → «Реквизиты для перевода».")
        lines.append("Тестовой оплаты нет: кнопок теста клиенты не видят.")
        other = "manual" if mode == "stars" else "stars"
        rows += [[cb(MODE_TO[other], AdminCb(s="s", a="pm_ask", v=other))],
                 [cb(MODE_ON["test"], AdminCb(s="s", a="pm_ask", v="test"))]]
        if any((await maintenance.test_data_counts(session)).values()):
            lines.append("🧹 Остались тестовые заказы и история (🧪): их можно удалить в «💾 Данные».")
            rows.append([cb("🧹 Удалить тестовые данные", AdminCb(s="d", a="clear"))])
    rows.append([cb("⬅️ Назад", AdminCb(s="s", a="grp", v="pay")), menu_btn()])
    return "\n".join(lines), ik(*rows)


def need_details_view() -> tuple[str, InlineKeyboardMarkup]:
    return ("🏦 <b>Сначала укажи реквизиты для перевода</b>\nИх клиент увидит в счёте: карта, СБП, банк, получатель. "
            "Потом включай оплату переводом.",
            ik([cb("✏️ Указать реквизиты", AdminCb(s="s", a="key", v="manual_pay_details"))],
               [cb("⬅️ Назад", AdminCb(s="s", a="pm")), menu_btn()]))


async def confirm_text(session: AsyncSession, s: RuntimeSettings, target: str) -> str:
    current = s.payments_mode
    if target == "test":
        n = await _real_pending(session)
        return "\n".join([
            "🧪 <b>Вернуть тестовую оплату?</b>",
            f"• Новые счета станут тестовыми: под ними кнопка «{PAY_TEST_BTN}», деньги воображаемые.",
            "• Настоящие оплаты не принимаются, пока снова не включишь звёзды или перевод."
            + (f" Уже выставленные настоящие счета ({n}) остаются в силе: если клиент оплатит, деньги зачислятся "
               "как обычно." if n else ""),
            "⚠️ Пока включён тест, любой клиент может «оплатить» заказ воображаемыми деньгами: включай его только "
            "для проверки бота."])
    how = ("звёздами прямо в Telegram (курс " + _rate(s) + ")" if target == "stars"
           else "переводом по реквизитам с проверкой чека")
    title = {"stars": "⭐ <b>Включить оплату звёздами?</b>", "manual": "🏦 <b>Включить оплату переводом?</b>"}[target]
    if current != "test":  # между настоящими способами - тестовых денег уже нет
        return "\n".join([title, f"Новые счета: {how}.",
                          "Уже выставленные счета старым способом остаются в силе."])
    left = await paymode.leftovers(session)
    lines = [title, "Тестовая оплата исчезнет полностью:",
             f"• новые счета: только {how}; кнопки «{PAY_TEST_BTN}» больше не будет;"]
    if left.invoices:
        lines.append(f"• неоплаченные тестовые счета отменятся: <b>{left.invoices}</b>, их кнопки перестанут "
                     "работать, клиент просто нажмёт «💳 Оплатить» в заказе ещё раз;")
    if left.rub or left.bonus:
        money = " и ".join(x for x in (f"<b>{fmt_rub(left.rub)}</b>" if left.rub else "",
                                        f"<b>{fmt_rub(left.bonus)}</b> бонусами" if left.bonus else "") if x)
        lines.append(f"• тестовые деньги уберутся с балансов: {money} (клиентов: {left.users}). "
                     "Настоящие рубли и бонусы не трогаются;")
    if left.orders_open:
        lines.append(f"• тестовые заказы в очереди и в работе ({left.orders_open}) останутся с пометкой 🧪: их можно "
                     "доделать или удалить в «💾 Данные → 🧹 Очистить тестовые данные».")
    lines += ["", "Вернуть тест можно в любой момент: «⚙️ Настройки → 💳 Оплата → Режим оплаты»."]
    return "\n".join(lines)


@router.callback_query(_f("pm"))
async def pm_screen(call: CallbackQuery, state: FSMContext, session: AsyncSession, s: RuntimeSettings) -> None:
    await state.clear()
    await call.answer()
    await show(call, *await pm_view(session, s))


@router.callback_query(_f("pm_ask"))
async def pm_ask(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    target = callback_data.v
    if target not in paymode.MODES:
        await call.answer("Нет такого режима")
        return
    await call.answer()
    if target == s.payments_mode:
        await show(call, *await pm_view(session, s, f"ℹ️ Уже включено: {mode_label(target)}."))
        return
    if target == "manual" and _no_details(s):
        await show(call, *need_details_view())
        return
    yes = {"stars": "✅ Да, включить звёзды", "manual": "✅ Да, включить перевод", "test": "✅ Да, вернуть тест"}
    await show(call, await confirm_text(session, s, target),
               ik([cb(yes[target], AdminCb(s="s", a="pm_go", v=target))],
                  [cb("✖️ Отмена", AdminCb(s="s", a="pm"))]))


@router.callback_query(_f("pm_go"))
async def pm_go(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    target = callback_data.v
    if target not in paymode.MODES:
        await call.answer("Нет такого режима")
        return
    if target == s.payments_mode:
        await call.answer()
        await show(call, *await pm_view(session, s, f"ℹ️ Уже включено: {mode_label(target)}."))
        return
    if target == "manual" and _no_details(s):
        await call.answer()
        await show(call, *need_details_view())
        return
    removed = await paymode.switch_mode(session, s.payments_mode, target, call.from_user.id, now_utc())
    note = [MODE_DONE[target]]
    if removed.invoices:
        note.append(f"Отменено тестовых счетов: {removed.invoices}.")
    if removed.rub or removed.bonus:
        note.append(f"Убрано тестовых денег: {fmt_rub(removed.rub)} и {fmt_rub(removed.bonus)} бонусами "
                    f"(клиентов: {removed.users}).")
    await call.answer(f"Режим оплаты: {mode_label(target)}")
    await show(call, *await pm_view(session, await load_settings(session), " ".join(note)))
