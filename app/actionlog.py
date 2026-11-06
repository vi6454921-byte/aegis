"""v2.4: понятный журнал действий пользователей и админов (логгер «aegis.actions», уровень INFO).

Одна строка на действие, её видно в stdout (Botkeep) и в data/logs/aegis.log:

    2026-11-05 19:32:08 | @username (id 123) | нажал «💰 Баланс»

Время по Самаре (формат задаёт app/logging_setup.py). Кто действует, берётся из контекста апдейта
(ActionLogMiddleware) или передаётся явно. В журнал не попадают: содержимое сообщений (только тип и длина),
промокоды (маска AEG-****-12), токен, платёжные данные (charge id, реквизиты)."""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Any

from app.utils.money import NBSP, fmt_rub, plural

log = logging.getLogger("aegis.actions")
_actor: ContextVar[Any] = ContextVar("aegis_action_actor", default=None)

CODE_RE = re.compile(r"\bAEGIS[\s_\-]*[A-Z0-9]{4}[\s_\-]*[A-Z0-9]{4}\b", re.IGNORECASE)
CHARS = ("символ", "символа", "символов")


# ------------------------------------------------------------------ кто и как
def who(user: Any) -> str:
    """«@username (id 123)» или «без username (id 123)»; без пользователя: «система»."""
    if user is None:
        return "система"
    uid = getattr(user, "id", None)
    name = getattr(user, "username", None)
    return f"{'@' + name if name else 'без username'} (id {uid})"


def mask_code(code: str | None) -> str:
    """AEGIS-ABCD-2345 → AEG-****-45: по маске нельзя восстановить код."""
    raw = re.sub(r"[\s_\-]+", "", (code or "").strip())
    if len(raw) < 6:
        return "****"
    return f"{raw[:3].upper()}-****-{raw[-2:].upper()}"


def hide_codes(text: str) -> str:
    """Промокоды в тексте (например, на кнопке админки) заменяет маской."""
    return CODE_RE.sub(lambda m: mask_code(m.group(0)), text)


def rub(kop: int | None) -> str:
    return fmt_rub(int(kop or 0)).replace(NBSP, " ")


def chars(n: int) -> str:
    return f"{n} {plural(n, CHARS)}"


def set_actor(user: Any):  # noqa: ANN201
    return _actor.set(user)


def reset_actor(token) -> None:  # noqa: ANN001
    _actor.reset(token)


def actor() -> Any:
    return _actor.get()


class _Someone:
    def __init__(self, uid: int | None, username: str | None = None):
        self.id, self.username = uid, username


def by_id(user_id: int | None) -> Any:
    """Текущий пользователь апдейта, если это он; иначе «без username (id N)»."""
    current = _actor.get()
    if current is not None and getattr(current, "id", None) == user_id:
        return current
    return _Someone(user_id) if user_id is not None else None


def action(text: str, user: Any = None, level: int = logging.INFO) -> None:
    """Пишет строку «кто | что». user=None: тот, чей апдейт сейчас обрабатывается."""
    try:
        log.log(level, "%s | %s", who(user if user is not None else _actor.get()),
                hide_codes(" ".join(str(text).split())))
    except Exception:  # noqa: BLE001 - журнал не должен ломать работу бота
        logging.getLogger(__name__).debug("не удалось записать действие", exc_info=True)


# ------------------------------------------------------------------ события бизнес-логики
def order_created(user: Any, order: Any) -> None:
    from app import texts  # noqa: PLC0415 - texts тяжёлый, нужен только здесь

    subject = texts.SUBJECTS.get(order.subject, order.subject)
    if order.subject == "design":  # v2.4: «🎨 Файлы и дизайн»: тип, оформление, количество
        from app.services.design import describe_plain  # noqa: PLC0415

        price = "по безлимиту" if getattr(order, "unlimited", False) else f"цена {rub(order.price_final or 0)}"
        action(f"создал заказ #{order.id} ({subject}: {describe_plain(order)}), {price}", user)
        return
    if order.subject == "any":
        action(f"создал заказ #{order.id} ({subject}), ждёт оценки", user)
        return
    count = getattr(order, "task_count", None) or 0
    what = f", задач: {count}" if count else ""
    action(f"создал заказ #{order.id} ({subject}{what}), цена {rub(order.price_final or order.price)}", user)


def order_paid(user: Any, order: Any, payment: Any) -> None:
    parts = []
    if getattr(order, "paid_bonus", 0):
        parts.append(f"бонусами {rub(order.paid_bonus)}")
    if getattr(order, "paid_balance", 0):
        parts.append(f"с баланса {rub(order.paid_balance)}")
    if getattr(order, "paid_invoice", 0):
        parts.append(f"счётом {rub(order.paid_invoice)}")
    how = f" ({', '.join(parts)})" if parts else ""
    test = ", тестовая оплата" if getattr(payment, "is_test", False) else ""
    what = ""
    if getattr(order, "subject", None) == "design":
        from app.services.design import describe_plain  # noqa: PLC0415

        what = f" ({describe_plain(order)})"
    action(f"оплатил заказ #{order.id}{what}: {rub(order.price_final)}{how}{test}", user)


def topup(user: Any, payment: Any) -> None:
    bonus = f" + бонус {rub(payment.topup_bonus)}" if getattr(payment, "topup_bonus", 0) else ""
    test = ", тестовая оплата" if getattr(payment, "is_test", False) else ""
    action(f"пополнил баланс: {rub(payment.amount_rub)}{bonus}{test}", user)


def to_balance(user: Any, payment: Any, order: Any) -> None:
    action(f"оплата {rub(payment.amount_rub)} зачислена на баланс: заказ #{order.id} уже не ждал оплаты", user)


# откуда деньги и бонусы: source партии бонусов или kind строки журнала transactions
KINDS = {
    "referral": "за заказ друга", "ref_channel": "друг подписался на канал", "channel": "за подписку на канал",
    "channel_bonus_revoke": "отписка от канала", "referral_revoke": "возврат заказа друга",
    "topup_bonus": "за пополнение", "refund": "возврат за заказ", "stars_refund": "возврат Stars",
    "admin": "от админа", "admin_credit": "от админа", "admin_topup": "от админа",
    "admin_debit": "списал админ", "admin_withdraw": "списал админ",
    "profile_link": "за ссылку в профиле", "profile_link_revoke": "ссылка из профиля убрана",
    "promo_post": "за пост", "promo_views": "за просмотры", "promo_adjust": "поправка за пост",
    "test_off": "тестовые деньги убраны",
}
# эти движения уже видны в строке об оплате или пополнении
SILENT_KINDS = ("topup", "payment_in", "order_pay")


def money(user: Any, account: str, amount: int, kind: str) -> None:
    """Движение по балансу (account="rub") или бонусам ("bonus"); amount со знаком, в копейках."""
    if kind in SILENT_KINDS or not amount:
        return
    what = "бонусы" if account == "bonus" else "баланс"
    sign = "+" if amount > 0 else "−"
    action(f"{what} {sign}{rub(abs(amount))} ({KINDS.get(kind, kind)})", user)


def promo_applied(user: Any, code: str | None, result: str) -> None:
    action(f"ввёл промокод {mask_code(code)}: {result}", user)


# действия админов: action из admin_log → по-человечески; детали только из белого списка, без кодов и текстов
ADMIN_ACTIONS = {
    "issue_invoice": "выставил счёт", "set_price": "поменял цену", "set_task_count": "поменял число задач",
    "note_approve": "одобрил конспект", "note_reject": "не принял конспект", "back_to_review": "вернул на проверку",
    "reject_order": "отклонил заказ", "take_in_work": "взял в работу", "upload_solution": "загрузил решение",
    "mark_done": "выдал решение", "message_client": "написал клиенту", "redo": "отправил на переделку",
    "refund": "сделал возврат", "pay_reminder": "напомнил об оплате", "quote_price": "назначил цену",
    "adjust_balance": "изменил баланс", "give_money": "начислил деньги", "manual_confirm": "подтвердил перевод",
    "manual_reject": "не подтвердил перевод", "broadcast": "запустил рассылку", "backup_now": "сделал бэкап",
    "export_csv": "выгрузил CSV", "export_users_xlsx": "выгрузил пользователей", "set_setting": "поменял настройку",
    "ban": "забанил", "unban": "разбанил", "message_user": "написал пользователю", "db_report": "открыл отчёт",
    "set_priority": "поменял приоритет", "set_free_month": "поменял бесплатный месяц",
    "promo_export": "выгрузил промокоды", "promo_generate": "создал промокоды", "promo_revoke": "отозвал промокод",
    "unlimited_grant": "выдал безлимит", "unlimited_remove": "снял безлимит", "clear_test_data": "очистил тест",
    "payments_mode": "поменял режим оплаты", "admin_set_level": "назначил уровень админа",
    "admin_remove": "снял админа", "admin_invite": "создал приглашение админа",
    "admin_invite_used": "принял приглашение в админы", "pinned_bind": "закреплён как админ 2 уровня",
    "pinned_spoof": "чужой ник закреплённого админа", "pinned_restore": "вернул закреплённого админа",
    "pinned_env_sync": "синхронизация закреплённых админов", "profile_approve": "одобрил профиль",
    "profile_reject": "отклонил профиль", "profile_revoke": "снял награду за профиль",
    "promo_post_accept": "принял пост", "promo_views_approve": "засчитал просмотры", "promo_reject": "отклонил отчёт",
    "promo_post_deleted": "пост удалён", "promo_adjust": "поправил награду за пост",
    "promo_unlimited": "принял по безлимиту",
}
TARGETS = {"order": "заказ #{}", "user": "пользователь id {}", "payment": "платёж #{}",
           "promo_code": "промокод #{}", "promo_post": "пост #{}", "profile_claim": "заявка #{}",
           "setting": "настройка {}", "broadcast": "рассылка #{}"}
MONEY_KEYS = ("amount", "price", "rub", "old", "new", "requested", "bonus")
LABELS = {"amount": "сумма", "price": "цена", "rub": "рублями", "bonus": "бонусами", "old": "было", "new": "стало",
          "requested": "запрошено", "count": "штук", "kind": "вид", "days": "дней", "tasks": "задач",
          "level": "уровень", "method": "способ", "account": "счёт", "views": "просмотров", "value": "значение",
          "delivered": "доставлено", "user": "клиент id", "report": "отчёт", "mode": "режим", "files": "файлов"}
SECRET_KEYS = ("code", "codes", "comment", "url", "text", "charge_id", "token", "note", "username", "file")


def admin_action(admin_id: int | None, act: str, target_type: str | None, target_id: Any,
                 details: dict[str, Any]) -> None:
    """Строка о действии админа: что сделал, с кем или чем, суммы. Коды, тексты и комментарии не пишем."""
    user = by_id(admin_id) if admin_id else None
    text = f"админ: {ADMIN_ACTIONS.get(act, act)}"
    if target_type and target_id is not None:
        text += f", {TARGETS.get(target_type, target_type + ' {}').format(target_id)}"
    in_rub = act not in ("set_task_count", "set_setting", "admin_set_level", "admin_remove", "payments_mode")
    extra = []
    for key, value in details.items():
        if key in SECRET_KEYS or value is None or key not in LABELS and key != "reason":
            continue
        if key == "reason":
            extra.append(f"причина: {chars(len(str(value)))}")
        elif key == "files":
            extra.append(f"файлов: {len(value) if isinstance(value, (list, tuple)) else value}")
        elif key in MONEY_KEYS and in_rub and isinstance(value, int) and not isinstance(value, bool):
            if not (key == "requested" and value == details.get("amount")):
                extra.append(f"{LABELS[key]}: {rub(value)}")
        elif not isinstance(value, (dict, list, tuple)):
            extra.append(f"{LABELS[key]}: {value}")
    if act == "set_setting":
        extra = []  # значения настроек бывают длинными текстами
    if extra:
        text += f" ({', '.join(extra)})"
    action(text, user)


# ------------------------------------------------------------------ апдейты Telegram по-человечески
def _reply_buttons() -> set[str]:
    from app import texts  # noqa: PLC0415

    return {*texts.MAIN_MENU.values(), texts.ADMIN_MENU_BTN, texts.BTN_BACK, texts.BTN_CANCEL, texts.PHONE_BTN}


def _size(n: int | None) -> str:
    if not n:
        return "размер неизвестен"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")
    return f"{max(1, round(n / 1024))} КБ"


MEDIA = {"video": "видео", "voice": "голосовое", "audio": "аудио", "sticker": "стикер", "video_note": "кружок",
         "animation": "GIF", "location": "геопозицию", "venue": "место", "poll": "опрос", "dice": "кубик"}


def describe_message(msg: Any) -> str | None:
    if getattr(msg, "successful_payment", None) is not None:
        return None  # строку об оплате пишет сервис платежей (сумма, номер заказа)
    text = getattr(msg, "text", None)
    if text:
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command = parts[0].split("@", 1)[0][:32]
            return f"команда {command}" + (" (с параметром)" if len(parts) > 1 else "")  # параметр не пишем
        from app import texts  # noqa: PLC0415

        if text in _reply_buttons() or text.startswith(texts.DRAFT_MENU_PREFIX):
            return f"нажал «{text}»"
        return f"отправил текст ({chars(len(text))})"
    album = " из альбома" if getattr(msg, "media_group_id", None) else ""
    caption = f", подпись {chars(len(msg.caption))}" if getattr(msg, "caption", None) else ""
    if getattr(msg, "photo", None):
        return f"отправил фото{album}{caption}"
    doc = getattr(msg, "document", None)
    if doc is not None:
        name = doc.file_name or ""
        ext = name.rsplit(".", 1)[-1].upper()[:8] if "." in name else (doc.mime_type or "файл")
        return f"отправил файл{album} ({ext}, {_size(doc.file_size)}){caption}"
    if getattr(msg, "contact", None) is not None:
        return "отправил контакт"
    if getattr(msg, "web_app_data", None) is not None:
        return "отправил данные из Mini App"
    for attr, name in MEDIA.items():
        if getattr(msg, attr, None) is not None:
            return f"отправил {name}{caption}"
    return f"отправил сообщение ({getattr(msg, 'content_type', 'неизвестно')})"


def button_text(cq: Any) -> str | None:
    markup = getattr(getattr(cq, "message", None), "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or []:
        for button in row:
            if button.callback_data == cq.data:
                return button.text
    return None


def describe(update: Any) -> str | None:
    """Что сделал человек, одной фразой (без содержимого сообщений); None, если писать нечего."""
    if update.message is not None:
        return describe_message(update.message)
    if update.callback_query is not None:
        text = button_text(update.callback_query)
        return f"нажал «{text}»" if text else "нажал кнопку под старым сообщением"
    if update.edited_message is not None:
        return "изменил сообщение"
    if update.my_chat_member is not None and update.my_chat_member.chat.type == "private":
        status = update.my_chat_member.new_chat_member.status
        return "заблокировал бота" if status == "kicked" else "разблокировал бота"
    if update.chat_member is not None:
        old, new = update.chat_member.old_chat_member.status, update.chat_member.new_chat_member.status
        inside = ("member", "administrator", "creator", "restricted")
        if new in inside and old not in inside:
            return "подписался на канал"
        if old in inside and new not in inside:
            return "отписался от канала"
    return None
