"""Черновик заказа (v2.3): ничего не теряем.

Черновик — строка orders (status=draft) с файлами; сохраняется на каждом шаге. Уход в другой раздел, «/start»,
«✖️ Отмена» и перезапуск бота черновик не удаляют. Пока он есть, в нижнем меню — кнопка
«📝 Продолжить оформление заказа (фото: N)», а экран черновика предлагает «▶️ Продолжить» / «🗑 Удалить черновик».
Удаляется черновик только по кнопке (с подтверждением), после отправки заказа или через draft_ttl_days дней
без движения (за день до этого предупреждаем)."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import WizCb
from app.bot.keyboards import cb, ik, set_menu_draft
from app.db.models import Order, OrderFile
from app.services import orders as svc
from app.utils.timeutil import fmt_dt

# обработчик уже показал черновик (или сам о нём сказал) — middleware не дублирует «💾 Черновик сохранён»
_shown: ContextVar[bool] = ContextVar("aegis_draft_shown", default=False)


def mark_shown(flag: bool = True) -> None:
    _shown.set(flag)


def was_shown() -> bool:
    return _shown.get()


@dataclass
class DraftInfo:
    order: Order
    photos: int  # фото условия
    files: int  # все файлы (условие + конспекты)


async def draft_info(session: AsyncSession, user_id: int) -> DraftInfo | None:
    """Черновик, к которому есть смысл вернуться: с файлами или с текстом. Пустой — None."""
    order = await svc.get_draft(session, user_id)
    if order is None:
        return None
    rows = dict((await session.execute(select(OrderFile.kind, func.count(OrderFile.id))
                                       .where(OrderFile.order_id == order.id).group_by(OrderFile.kind))).all())
    files = sum(int(n) for n in rows.values())
    if not files and not (order.comment or "").strip():
        return None
    return DraftInfo(order, int(rows.get("condition", 0)), files)


async def refresh_menu(session: AsyncSession, user_id: int) -> DraftInfo | None:
    """Пересчитать кнопку черновика в нижнем меню для текущего апдейта."""
    info = await draft_info(session, user_id)
    set_menu_draft(info.photos if info else None)
    return info


def what(order: Order) -> str:
    if order.subject == svc.EXPLAIN:
        return texts.DRAFT_WHAT_EXPLAIN
    if order.subject in texts.SUBJECTS:
        return f"{texts.SUBJECT_ICONS.get(order.subject, '')} {texts.SUBJECTS[order.subject]}".strip()
    return texts.DRAFT_WHAT_NONE


def screen(info: DraftInfo, restart_kind: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Экран черновика: «▶️ Продолжить» / «🗑 Удалить черновик» (+ «🆕 Начать заново» из «📝 Заказать»)."""
    text = texts.DRAFT_SCREEN.format(what=what(info.order), n=info.photos, created=fmt_dt(info.order.created_at))
    rows = [[cb(texts.DRAFT_CONTINUE, WizCb(action="resume"))],
            [cb(texts.DRAFT_DELETE_BTN, WizCb(action="draft_del"))]]
    if restart_kind:
        rows[1].append(cb(texts.DRAFT_RESTART, WizCb(action="restart_ask", value=restart_kind)))
    return text, ik(*rows)


def last_activity(order: Order, last_file_at: datetime | None) -> datetime:
    times = [t for t in (order.updated_at, order.created_at, last_file_at) if t is not None]
    return max(times)
