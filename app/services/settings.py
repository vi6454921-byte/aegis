"""Настройки, которые админ меняет в боте. Хранятся в таблице settings (ключ → JSON)."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.db.models import Setting
from app.utils.money import fmt_mult, fmt_rub, parse_mult, parse_rub

BASE_DEFAULTS: dict[str, Any] = {
    "price_nach": 20000,
    "price_calc": 20000,
    "price_explain": 15000,
    "notes_discount_nach": True,
    "notes_discount_calc": False,
    "discount_lecture_pct": 25,
    "discount_practice_pct": 25,
    # Срочность: до дедлайна ≥ urgency_free_hours — множитель urgency_ge72_pct (без наценки),
    # от urgency_mid_hours до urgency_free_hours — urgency_24_72_pct, меньше urgency_mid_hours — urgency_lt24_pct.
    # (имена ключей исторические — значения у старых установок сохраняются)
    "urgency_free_hours": 48,
    "urgency_mid_hours": 24,
    "urgency_ge72_pct": 100,
    "urgency_24_72_pct": 150,
    "urgency_lt24_pct": 200,
    "max_tasks_per_day": 15,
    "show_slots_threshold": 5,
    "intake_mode": "auto",  # auto — по лимиту, closed — закрыт вручную, open — открыт вручную без лимита
    "unlimited_bypass_limit": False,  # безлимитные заказы (промокод) принимаются сверх дневного лимита
    "draft_ttl_days": 7,  # через сколько дней без движения удаляем черновик заказа (за день предупреждаем)
    "stray_timeout_minutes": 10,  # «Это задание?» без ответа — через столько минут отправляем админам
    "referral_bonus": 0,  # бонус за друга с оплаченным заказом; 0 — выключен (основной бонус — за подписку)
    "channel_username": "@aegis_samara",
    "channel_bonus_enabled": True,
    "channel_bonus": 5000,
    "channel_ref_bonus": 5000,
    "channel_hold_hours": 48,
    "channel_ref_daily_limit": 10,
    "priority_threshold": 5,
    "free_month_threshold": 10,
    "free_month_days": 30,
    "free_month_tasks": 10,
    "bonus_ttl_days": 30,
    "bonus_max_share_pct": 50,
    "rub_per_star": "1.75",
    "payments_mode": "test",
    "manual_pay_details": "",
    "pay_reminder_hours": 6,
    "admin_autosend_files": True,
    "topup_packages": [20000, 50000, 100000],
    "topup_bonus_enabled": False,
    "topup_bonus_map": {"50000": 5000, "100000": 15000},
    "stats_offset_done": 90,
    "stats_offset_first_try": 84,
    "welcome_text": texts.DEFAULT_WELCOME,
    "terms_text": texts.DEFAULT_TERMS,
    # подарки — ссылки в тексте приветствия и «🎁 Подарки» (названия — app/services/gifts.py); "" — скрыт
    "gift_url_calc": "https://fgdg346346hgjghj.github.io/Aegis-14/#z1a",
    "gift_url_nach": "https://fgdg346346hgjghj.github.io/Aegis-/#start",
    "gift_url_matrices": "https://fgdg346346hgjghj.github.io/matric/",
    # v2.2: задания «Ссылка в профиле» и «Пост о нас» — награды обычными рублями (копейки)
    "profile_task_enabled": True,
    "profile_task_reward": 10000,
    "profile_recheck_days": 0,  # 0 — не перепроверять
    "post_task_enabled": True,
    "post_base_reward": 20000,
    "post_views_step": 100,
    "post_views_reward": 10000,
    "post_views_cap": 0,  # потолок выплат за просмотры на один пост; 0 — без потолка
    "post_report_cooldown_hours": 12,
    "post_max_pending": 1,
    "post_max_active": 3,
    "post_template": texts.DEFAULT_POST_TEMPLATE,
    "withdraw_note": texts.WITHDRAW_SOON,
    # v2.2: выдача денег админом
    "admin_give_confirm_over": 1000000,  # больше 10 000 ₽ — повторное подтверждение
    "admin_give_show_comment": True,
}
REAL_PAYMENT_MODES = ("stars", "manual")  # настоящие деньги; test — воображаемые

_defaults: dict[str, Any] = dict(BASE_DEFAULTS)


def configure(config) -> None:  # noqa: ANN001
    """Стартовые значения из .env (MAX_TASKS_PER_DAY, RUB_PER_STAR, PAYMENTS_MODE...)."""
    _defaults.update(
        max_tasks_per_day=config.max_tasks_per_day,
        show_slots_threshold=config.show_slots_threshold,
        rub_per_star=str(config.rub_per_star),
        payments_mode=_mode(config.payments_mode),
        stats_offset_done=config.stats_offset_done,
        stats_offset_first_try=config.stats_offset_first_try,
    )


def _mode(raw: str) -> str:
    mode = (raw or "").strip().lower()
    return mode if mode in ("test", "stars", "manual") else "test"


class RuntimeSettings(dict):
    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(item) from e

    def price_for(self, subject: str) -> int:
        return int(self[f"price_{subject}"])

    def notes_discount_for(self, subject: str) -> bool:
        return bool(self.get(f"notes_discount_{subject}", False))

    @property
    def rate(self) -> Decimal:
        return Decimal(str(self["rub_per_star"]))

    @property
    def channel(self) -> str:
        """«@aegis_samara» или пусто — канал не настроен."""
        return str(self.get("channel_username") or "").strip()

    @property
    def channel_url(self) -> str | None:
        ch = self.channel
        return f"https://t.me/{ch.lstrip('@')}" if ch.startswith("@") else None

    @property
    def channel_bonus_on(self) -> bool:
        return bool(self.channel and self.get("channel_bonus_enabled") and int(self.get("channel_bonus") or 0) > 0)

    @property
    def channel_ref_on(self) -> bool:
        return bool(self.channel and self.get("channel_bonus_enabled") and int(self.get("channel_ref_bonus") or 0) > 0)


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    data = dict(_defaults)
    rows = (await session.execute(select(Setting))).scalars().all()
    for row in rows:
        if row.key in data:
            try:
                data[row.key] = json.loads(row.value)
            except ValueError:
                pass
    return RuntimeSettings(data)


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    """Без commit — вызывающий коммитит вместе с записью в admin_log."""
    if key not in _defaults:
        raise KeyError(key)
    row = await session.get(Setting, key)
    raw = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(Setting(key=key, value=raw))
    else:
        row.value = raw


# ------------------------------------------------------------------ для админки
# key: (тип, подпись)
META: dict[str, tuple[str, str]] = {
    "price_nach": ("money", "Цена задачи — начерталка"),
    "price_calc": ("money", "Цена задачи — расчётка"),
    "price_explain": ("money", "Цена объяснения материала"),
    "notes_discount_nach": ("bool", "Скидки за конспекты — начерталка"),
    "notes_discount_calc": ("bool", "Скидки за конспекты — расчётка"),
    "discount_lecture_pct": ("pct", "Скидка за лекцию, %"),
    "discount_practice_pct": ("pct", "Скидка за практику, %"),
    "urgency_free_hours": ("int", "Без наценки, если до дедлайна от, ч"),
    "urgency_mid_hours": ("int", "Максимальная наценка, если меньше, ч"),
    "urgency_ge72_pct": ("mult", "Множитель «заранее»"),
    "urgency_24_72_pct": ("mult", "Множитель «скоро»"),
    "urgency_lt24_pct": ("mult", "Множитель «срочно»"),
    "max_tasks_per_day": ("int", "Лимит задач в день"),
    "show_slots_threshold": ("int", "Показывать остаток мест, когда ≤"),
    "intake_mode": ("enum:auto,closed,open", "Приём заказов"),
    "unlimited_bypass_limit": ("bool", "Безлимит обходит дневной лимит"),
    "draft_ttl_days": ("int", "Удалять черновик заказа через, дней"),
    "stray_timeout_minutes": ("int", "«Это задание?» без ответа — админам через, мин"),
    "referral_bonus": ("money", "Бонус за друга с оплаченным заказом (0 — выкл)"),
    "channel_username": ("str", "Канал"),
    "channel_bonus_enabled": ("bool", "Бонусы за подписку на канал"),
    "channel_bonus": ("money", "Бонус за подписку"),
    "channel_ref_bonus": ("money", "Бонус пригласившему за подписку друга"),
    "channel_hold_hours": ("int", "Удержание: отписка раньше — бонус списывается, ч"),
    "channel_ref_daily_limit": ("int", "Друзей за сутки на одного, не больше"),
    "priority_threshold": ("int", "Друзей до «Приоритета»"),
    "free_month_threshold": ("int", "Друзей до «Месяца бесплатно»"),
    "free_month_days": ("int", "«Месяц бесплатно»: дней"),
    "free_month_tasks": ("int", "«Месяц бесплатно»: задач"),
    "bonus_ttl_days": ("int", "Срок жизни бонусов, дней"),
    "bonus_max_share_pct": ("pct", "Доля оплаты бонусами, %"),
    "rub_per_star": ("decimal", "Курс: ₽ за 1 звезду"),
    "payments_mode": ("enum:test,stars,manual", "Режим оплаты"),
    "manual_pay_details": ("text", "Реквизиты для перевода"),
    "pay_reminder_hours": ("int", "Напомнить клиенту об оплате через, ч (0 — выкл)"),
    "admin_autosend_files": ("bool", "Сразу присылать админам файлы нового заказа"),
    "topup_packages": ("money_list", "Пакеты пополнения"),
    "topup_bonus_enabled": ("bool", "Бонус к пополнению"),
    "topup_bonus_map": ("money_map", "Бонус к пакетам"),
    "stats_offset_done": ("int", "Стартовое число сданных работ"),
    "stats_offset_first_try": ("int", "Из них с первой попытки"),
    "welcome_text": ("text", "Текст приветствия"),
    "terms_text": ("text", "Текст /terms"),
    "gift_url_calc": ("url", "🧮 Расчётка"),
    "gift_url_nach": ("url", "📐 Начерталка"),
    "gift_url_matrices": ("url", "🔢 Матрицы"),
    "profile_task_enabled": ("bool", "Задание «Ссылка в профиле»"),
    "profile_task_reward": ("money", "Награда за ссылку в профиле"),
    "profile_recheck_days": ("int", "Перепроверить ссылку через, дней (0 — выкл)"),
    "post_task_enabled": ("bool", "Задание «Пост о нас»"),
    "post_base_reward": ("money", "Награда за принятый пост"),
    "post_views_step": ("int", "Шаг просмотров"),
    "post_views_reward": ("money", "Сумма за каждый шаг просмотров"),
    "post_views_cap": ("money", "Потолок за просмотры на пост (0 — без потолка)"),
    "post_report_cooldown_hours": ("int", "Отчёт по посту не чаще, чем раз в, ч"),
    "post_max_pending": ("int", "Отчётов по посту на проверке одновременно"),
    "post_max_active": ("int", "Активных постов у пользователя, не больше"),
    "post_template": ("template", "Шаблон текста поста"),
    "withdraw_note": ("text", "Текст про вывод денег"),
    "admin_give_confirm_over": ("money", "Повторное подтверждение, если сумма больше"),
    "admin_give_show_comment": ("bool", "Показывать клиенту комментарий админа"),
}

GROUPS: dict[str, tuple[str, list[str]]] = {
    "prices": ("💵 Цены и скидки", ["price_nach", "price_calc", "price_explain", "notes_discount_nach",
                                    "notes_discount_calc", "discount_lecture_pct", "discount_practice_pct"]),
    "urgency": ("⏱ Срочность", ["urgency_free_hours", "urgency_mid_hours", "urgency_ge72_pct", "urgency_24_72_pct",
                                "urgency_lt24_pct"]),
    "slots": ("📦 Места и приём", ["intake_mode", "max_tasks_per_day", "show_slots_threshold",
                                    "unlimited_bypass_limit", "draft_ttl_days", "stray_timeout_minutes"]),
    "channel": ("📢 Канал и бонусы за подписку", ["channel_username", "channel_bonus_enabled", "channel_bonus",
                                                  "channel_ref_bonus", "channel_hold_hours",
                                                  "channel_ref_daily_limit"]),
    "ref": ("👥 Рефералка и бонусы", ["referral_bonus", "priority_threshold", "free_month_threshold",
                                       "free_month_days", "free_month_tasks", "bonus_ttl_days",
                                       "bonus_max_share_pct"]),
    "pay": ("💳 Оплата", ["payments_mode", "manual_pay_details", "rub_per_star", "topup_packages",
                          "topup_bonus_enabled", "topup_bonus_map"]),
    "notify": ("🔔 Уведомления", ["pay_reminder_hours", "admin_autosend_files"]),
    "texts": ("📝 Тексты и счётчик", ["welcome_text", "terms_text", "withdraw_note", "stats_offset_done",
                                      "stats_offset_first_try"]),
    "gifts": ("🎁 Подарки (ссылки)", ["gift_url_calc", "gift_url_nach", "gift_url_matrices"]),
    "tasks": ("🎯 Задания: профиль и посты", ["profile_task_enabled", "profile_task_reward", "profile_recheck_days",
                                             "post_task_enabled", "post_base_reward", "post_views_step",
                                             "post_views_reward", "post_views_cap", "post_report_cooldown_hours",
                                             "post_max_pending", "post_max_active", "post_template"]),
    "admin_money": ("💸 Деньги от админов", ["admin_give_confirm_over", "admin_give_show_comment"]),
}

ENUM_LABELS = {
    "intake_mode": {"auto": "по лимиту", "closed": "закрыт вручную", "open": "открыт вручную (без лимита)"},
    "payments_mode": {"test": "🧪 тест", "stars": "⭐ Telegram Stars", "manual": "🏦 перевод (ручная проверка)"},
}


def setting_type(key: str) -> str:
    return META[key][0]


def is_texty(key: str) -> bool:
    """Длинный текст: показывается курсивом и правится отдельным сообщением."""
    return setting_type(key) in ("text", "template")


def format_value(key: str, value: Any) -> str:
    kind = setting_type(key)
    if kind == "money":
        return fmt_rub(int(value))
    if kind == "bool":
        return "вкл" if value else "выкл"
    if kind == "pct":
        return f"{value}%"
    if kind == "mult":
        return fmt_mult(int(value))
    if kind.startswith("enum:"):
        return ENUM_LABELS.get(key, {}).get(value, str(value))
    if kind == "money_list":
        return " / ".join(fmt_rub(int(v)) for v in value)
    if kind == "money_map":
        return ", ".join(f"{fmt_rub(int(k))} → +{fmt_rub(int(v))}" for k, v in value.items()) or "—"
    if kind in ("text", "template"):
        one = str(value).replace("\n", " ").strip()
        if not one:
            return "—"
        return (one[:60] + "…") if len(one) > 60 else one
    if kind == "str":
        return str(value) or "— (выключено)"
    if kind == "url":
        return re.sub(r"^https?://", "", str(value or "")) or "— (скрыт)"
    return str(value)


def cycle_enum(key: str, value: str) -> str:
    options = setting_type(key).split(":", 1)[1].split(",")
    return options[(options.index(value) + 1) % len(options)] if value in options else options[0]


def parse_value(key: str, text: str) -> Any:
    """Разбор ввода админа. Бросает ValueError с понятным текстом."""
    kind = setting_type(key)
    t = (text or "").strip()
    if kind == "money":
        if ZERO_RE.fullmatch(t) and key in MONEY_ZERO_OK:
            return 0
        v = parse_rub(t)
        if v is None:
            raise ValueError("Нужна сумма в рублях, например 200" + (" (или 0 — выключить)" if key in MONEY_ZERO_OK
                                                                    else ""))
        if v > MONEY_SETTING_MAX:
            raise ValueError(f"Слишком большая сумма (максимум {fmt_rub(MONEY_SETTING_MAX)})")
        return v
    if kind in ("int", "pct"):
        if not t.isdigit():
            raise ValueError("Нужно целое число")
        v = int(t)
        if kind == "pct" and not 0 <= v <= 100:
            raise ValueError("Процент — от 0 до 100")
        lo, hi = INT_LIMITS.get(key, (0, 10**6))
        if v < lo:
            raise ValueError(f"Не меньше {lo}")
        if v > hi:
            raise ValueError(f"Не больше {hi}")
        return v
    if kind == "str":
        return parse_channel(t)
    if kind == "url":
        return parse_url(t)
    if kind == "mult":
        v = parse_mult(t)
        if v is None:
            raise ValueError("Нужен множитель, например 1,5")
        return v
    if kind == "decimal":
        try:
            d = Decimal(t.replace(",", "."))
        except InvalidOperation:
            raise ValueError("Нужно число, например 1,75") from None
        if d <= 0:
            raise ValueError("Должно быть больше нуля")
        return str(d)
    if kind == "money_list":
        values = [parse_rub(p) for p in t.replace(";", ",").replace("/", ",").split(",") if p.strip()]
        if not values or any(v is None for v in values) or len(values) > 6:
            raise ValueError("Перечисли суммы через запятую, например 200, 500, 1000")
        return values
    if kind == "money_map":
        result: dict[str, int] = {}
        for part in t.replace(";", ",").split(","):
            if not part.strip():
                continue
            if "=" not in part:
                raise ValueError("Формат: 500=50, 1000=150")
            k, v = part.split("=", 1)
            kk, vv = parse_rub(k), parse_rub(v)
            if kk is None or vv is None:
                raise ValueError("Формат: 500=50, 1000=150")
            result[str(kk)] = vv
        return result
    if kind == "template":
        if not t:
            raise ValueError("Текст не может быть пустым")
        if "{link}" not in t:
            raise ValueError("Добавь в текст {link} — туда подставится ссылка на бота")
        if len(t) > 2000:
            raise ValueError("Слишком длинно (максимум 2000 символов)")
        return t
    if kind == "text":
        if key in TEXT_CLEARABLE and t in ("-", "—", "нет", "очистить"):
            return ""
        if not t:
            raise ValueError("Текст не может быть пустым")
        if len(t) > 3500:
            raise ValueError("Слишком длинно (максимум 3500 символов)")
        return text
    raise ValueError("Это значение меняется кнопкой")


ZERO_RE = re.compile(r"0+(?:[.,]0+)?")
MONEY_SETTING_MAX = 10**15  # 10 трлн ₽ в копейках: с запасом до предела BIGINT, чтобы суммы не переполняли балансы
MONEY_ZERO_OK = {"referral_bonus", "channel_bonus", "channel_ref_bonus", "post_views_cap"}
TEXT_CLEARABLE = {"manual_pay_details"}
INT_LIMITS: dict[str, tuple[int, int]] = {
    "max_tasks_per_day": (0, 1000),
    "show_slots_threshold": (0, 1000),
    "draft_ttl_days": (1, 90),
    "stray_timeout_minutes": (1, 24 * 60),
    "bonus_ttl_days": (1, 3650),
    "free_month_days": (1, 3650),
    "free_month_tasks": (0, 1000),
    "priority_threshold": (1, 10000),
    "free_month_threshold": (1, 10000),
    "urgency_free_hours": (2, 24 * 30),
    "urgency_mid_hours": (1, 24 * 30),
    "channel_hold_hours": (0, 24 * 30),
    "channel_ref_daily_limit": (1, 1000),
    "pay_reminder_hours": (0, 24 * 7),
    "profile_recheck_days": (0, 3650),
    "post_views_step": (1, 10**9),
    "post_report_cooldown_hours": (0, 24 * 30),
    "post_max_pending": (1, 10),
    "post_max_active": (1, 100),
}
CHANNEL_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


def parse_channel(text: str) -> str:
    """«@name», «t.me/name», «https://t.me/name» → «@name»; «-» — выключить."""
    t = (text or "").strip()
    if t in ("-", "—", "нет", "выкл", "0"):
        return ""
    t = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/", "", t, flags=re.IGNORECASE).strip("/").lstrip("@")
    if not CHANNEL_RE.fullmatch(t):
        raise ValueError("Нужен публичный канал: @username или ссылка t.me/username (или «-» — выключить)")
    return "@" + t


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
OFF_WORDS = ("-", "—", "нет", "выкл", "0")


def parse_url(text: str) -> str:
    """Ссылка на подарок: https://… (без схемы — допишем https://); «-» — скрыть подарок."""
    t = (text or "").strip()
    if t.lower() in OFF_WORDS:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", t, re.IGNORECASE):
        t = "https://" + t
    try:
        host = (urlsplit(t).hostname or "") if URL_RE.fullmatch(t) else ""
    except ValueError:  # кривой адрес вроде https://[abc
        host = ""
    if "." not in host or len(t) > 1000:
        raise ValueError("Нужна ссылка вида https://…, например https://username.github.io/page/ "
                         "(или «-» — скрыть подарок)")
    return t


def default_value(key: str) -> Any:
    """Значение по умолчанию (с учётом .env) — для кнопки «Сбросить»."""
    return _defaults[key]
