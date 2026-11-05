"""Тексты, которые собираются из настроек: сроки и наценки, цены, бонусы, помощь и обучалка.

Всё, что зависит от цифр в админке (множители срочности, пороги в часах, бонусы, канал), живёт здесь —
так подсказки на кнопках, «❓ Помощь» и обучалка всегда совпадают с тем, как реально считается цена.
"""
from __future__ import annotations

import html

from app import texts
from app.services.pricing import urgency_from_settings
from app.services.settings import RuntimeSettings
from app.utils.money import fmt_mult, fmt_rub, plural

TUTORIAL_PAGES = 5


# ------------------------------------------------------------------ срочность
def _less_than(hours: int) -> str:
    """24 → «Меньше суток», 48 → «Меньше 2 дней», 12 → «Меньше 12 ч»."""
    if hours == 24:
        return "Меньше суток"
    if hours % 24 == 0:
        d = hours // 24
        return f"Меньше {d} {plural(d, ('дня', 'дней', 'дней'))}"
    return f"Меньше {hours} ч"


def _range(lo: int, hi: int) -> str:
    """24..48 → «1–2 дня», 12..48 → «12–48 ч»."""
    if lo % 24 == 0 and hi % 24 == 0:
        d = hi // 24
        return f"{lo // 24}–{d} {plural(d, ('день', 'дня', 'дней'))}"
    return f"{lo}–{hi} ч"


def markup_label(pct: int) -> str:
    return texts.NO_MARKUP if int(pct) == 100 else fmt_mult(int(pct))


def urgency_lines(s: RuntimeSettings) -> list[str]:
    free, mid = int(s.urgency_free_hours), int(s.urgency_mid_hours)
    early, soon, urgent = int(s.urgency_ge72_pct), int(s.urgency_24_72_pct), int(s.urgency_lt24_pct)
    if free % 24 == 0:
        d = free // 24
        first = f"⏰ За {d}+ {plural(d, ('день', 'дня', 'дней'))} до дедлайна — {markup_label(early)}"
    else:
        first = f"⏰ За {free}+ ч до дедлайна — {markup_label(early)}"
    lines = [first]
    if mid < free:
        lines.append(f"⚡ За {_range(mid, free)} — {markup_label(soon)}")
    lines.append(f"🔥 {_less_than(mid)} — {markup_label(urgent)}")
    return lines


def urgency_short(s: RuntimeSettings) -> str:
    """Одной строкой для шага «дедлайн»."""
    return " · ".join(urgency_lines(s))


def deadline_hint(s: RuntimeSettings, hours: float) -> str:
    return markup_label(urgency_from_settings(s, hours))


# ------------------------------------------------------------------ цены и бонусы
def price_lines(s: RuntimeSettings) -> list[str]:
    lines = [f"📐 Начерталка — {fmt_rub(int(s.price_nach))} за задачу",
             f"🧮 Расчётка по вышмату — {fmt_rub(int(s.price_calc))} за задачу",
             f"📸 Объяснение материала — {fmt_rub(int(s.price_explain))}"]
    notes = [name for code, name in (("nach", "начерталка"), ("calc", "расчётка")) if s.notes_discount_for(code)]
    if notes:
        lines.append(f"📚 Скидка за фото конспектов ({', '.join(notes)}): лекция −{s.discount_lecture_pct}%, "
                     f"практика −{s.discount_practice_pct}% — после нашей проверки")
    return lines


def task_lines(s: RuntimeSettings) -> list[str]:
    """Задания v2.2 — рубли на баланс (не бонусы): они не сгорают и оплачивают заказ целиком."""
    lines = []
    if s.profile_task_enabled and int(s.profile_task_reward) > 0:
        lines.append(f"📌 Ссылка на бота в профиле Telegram — +{fmt_rub(int(s.profile_task_reward))} "
                     "на рублёвый баланс (один раз)")
    if s.post_task_enabled:
        step = int(s.post_views_step)
        lines.append(f"📣 Пост о нас в Telegram-канале или ВКонтакте — +{fmt_rub(int(s.post_base_reward))} рублями "
                     f"и ещё {fmt_rub(int(s.post_views_reward))} за каждые {step} просмотров")
    return lines


def bonus_lines(s: RuntimeSettings) -> list[str]:
    lines = []
    if s.channel_bonus_on:
        lines.append(f"📢 Подписка на канал {html.escape(s.channel)} — +{fmt_rub(int(s.channel_bonus))} бонусами "
                     f"(по желанию, один раз)")
    if s.channel_ref_on:
        lines.append(f"👥 Друг пришёл по твоей ссылке и подписался на канал (и ты подписан) — вам обоим по "
                     f"{fmt_rub(int(s.channel_ref_bonus))}")
    if int(s.referral_bonus) > 0:
        lines.append(f"💳 Друг оплатил и получил первый заказ — тебе ещё +{fmt_rub(int(s.referral_bonus))}")
    lines += task_lines(s)
    lines.append(f"⭐ {s.priority_threshold} друзей с оплаченным заказом — «Приоритет», "
                 f"{s.free_month_threshold} — «Месяц бесплатно»")
    lines.append(f"💰 Бонусами можно оплатить до {s.bonus_max_share_pct}% цены заказа, они действуют "
                 f"{s.bonus_ttl_days} {plural(int(s.bonus_ttl_days), ('день', 'дня', 'дней'))}")
    return lines


# ------------------------------------------------------------------ «❓ Помощь»
def help_text(s: RuntimeSettings) -> str:
    notes = " и конспекты" if (s.notes_discount_for("nach") or s.notes_discount_for("calc")) else ""
    lines = [
        texts.HELP_TITLE,
        "<b>Как это работает</b>",
        "1. «📝 Заказать» → предмет, фото условия, количество задач и вариант, дедлайн.",
    ]
    if notes:
        lines.append("2. По желанию добавь фото лекции и практики по теме — скидка до "
                     f"−{int(s.discount_lecture_pct) + int(s.discount_practice_pct)}% (для начерталки).")
    else:
        lines.append("2. Проверь итог и нажми «🚀 Отправить» — цена видна сразу.")
    lines += [
        f"3. Мы проверяем условия{notes} и присылаем счёт.",
        "4. После оплаты делаем разбор: 3D-модель, полное решение и алгоритм — открывается прямо в Telegram.",
        "5. Не зачли — жми «❌ Не зачли», пришли скрин замечания: переделаем или вернём деньги.",
        "",
        "📸 Не понял тему с лекции? «📸 Объяснить материал» — пришли фото конспекта, объясним простыми словами.",
        "",
        "<b>Сроки и наценка</b>",
        *urgency_lines(s),
        "",
        texts.HELP_COMMANDS_LINE,
        "Выбери, что подсказать 👇",
    ]
    return "\n".join(lines)


def prices_text(s: RuntimeSettings) -> str:
    return "\n".join([
        "⏰ <b>Сроки и цены</b>",
        *price_lines(s),
        "",
        "<b>Наценка за срочность</b> — по времени до дедлайна в момент отправки заказа:",
        *urgency_lines(s),
        "",
        "💡 Чем раньше пришлёшь задачу — тем дешевле. Итоговая цена видна до отправки заказа.",
    ])


def bonuses_text(s: RuntimeSettings) -> str:
    lines = ["🎁 <b>Бонусы и акции</b>", *bonus_lines(s), "",
             "Бонусы и рубли видно в «💰 Баланс», ссылку для друзей — в «👥 Пригласить друга»."]
    if task_lines(s):
        lines.append("Задания за рубли — кнопками ниже 👇")
    return "\n".join(lines + ["", s.withdraw_note])


GUARANTEE_TEXT = (
    "🛡 <b>Гарантия</b>\n"
    "• Не зачли — открой заказ в «📦 Мои заказы», нажми «❌ Не зачли» и пришли скрин замечания преподавателя.\n"
    "• Мы переделаем разбор с учётом замечания или вернём деньги — на баланс бота, а если платил звёздами, "
    "то звёздами.\n"
    "• Рублёвый баланс не сгорает — им можно оплатить следующие заказы целиком."
)

PAYMENT_TEXT = (
    "💳 <b>Оплата</b>\n"
    "• Счёт приходит после проверки условия — сюда, в чат, с кнопкой «💳 Оплатить».\n"
    "• Сначала списываются бонусы (до {share}% цены), потом рублёвый баланс, остаток — счётом.\n"
    "• Работа начинается сразу после оплаты. Вопросы по оплате — /paysupport."
)


# ------------------------------------------------------------------ обучалка
def tutorial_page(page: int, s: RuntimeSettings) -> str:
    n = TUTORIAL_PAGES
    head = f"📖 <b>Как пользоваться ботом</b> · {page}/{n}\n" + "▰" * page + "▱" * (n - page) + "\n\n"
    m = texts.MAIN_MENU
    if page == 1:
        body = ("Всё управление — кнопками внизу экрана 👇\n\n"
                f"{m['order']} — разбор задач: 3D-модель, решение и алгоритм\n"
                f"{m['explain']} — объясним тему по фото лекции или практики\n"
                f"{m['my_orders']} — статусы, счета и готовые решения\n"
                f"{m['balance']} — рубли, бонусы и история\n"
                f"{m['invite']} — ссылка для друзей и бонусы\n"
                f"{m['gifts']} — бесплатные материалы (ссылки) и бонус за подписку\n"
                f"{m['help']} — ответы на вопросы и поддержка\n\n"
                "<i>Если кнопки пропали — нажми значок клавиатуры в поле ввода или отправь /start.</i>")
    elif page == 2:
        notes = s.notes_discount_for("nach")
        body = ("<b>Как заказать разбор</b>\n"
                f"1️⃣ Нажми «{m['order']}» и выбери предмет.\n"
                "2️⃣ Пришли фото условия (можно несколько) и нажми «✅ Готово».\n"
                "3️⃣ Укажи, сколько задач, номер варианта и дедлайн.\n"
                + ("4️⃣ По желанию — фото лекции и практики: это скидка.\n" if notes else "")
                + f"{'5️⃣' if notes else '4️⃣'} Проверь итог — цена видна сразу — и нажми «🚀 Отправить».\n\n"
                "Мы проверяем условия и конспекты и присылаем счёт. После оплаты решение придёт сюда — "
                "кнопка «🧩 Открыть решение».")
    elif page == 3:
        body = ("<b>Не понял тему? Объясним</b>\n"
                f"Нажми «{m['explain']}», пришли фото лекции или практики и напиши, что непонятно — "
                "или нажми «📚 Объясните всю тему».\n\n"
                f"Стоимость — {fmt_rub(int(s.price_explain))}, счёт пришлём после проверки материала. "
                "Объяснение придёт прямо в чат, а если останутся вопросы — жми «💬 Задать вопрос».")
    elif page == 4:
        body = "<b>Цены и сроки</b>\n" + "\n".join(price_lines(s)) + "\n\n" + "\n".join(urgency_lines(s)) + \
               "\n\n💡 Чем раньше пришлёшь задачу, тем дешевле."
    else:
        body = ("<b>Бонусы, акции и гарантия</b>\n" + "\n".join(bonus_lines(s)) +
                "\n\n🛡 Не зачли — переделаем или вернём деньги.\n"
                "💬 Вопросы — «❓ Помощь» → «💬 Поддержка», отвечаем здесь же.\n\n" + s.withdraw_note)
    return head + body
