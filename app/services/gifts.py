"""Подарки — бесплатные материалы по ссылкам (сейчас — страницы на GitHub Pages).

Показываются только ссылками прямо в тексте сообщений: в приветствии (/start, подстановка {gifts})
и в «🎁 Подарки». Адреса меняются в админке: ⚙️ Настройки → 🎁 Подарки (ссылки); «-» — скрыть подарок.
Чтобы переименовать подарок или добавить новый — поправь GIFTS ниже и BASE_DEFAULTS/META в settings.py.
"""
from __future__ import annotations

import html
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.db.models import GiftLink


@dataclass(frozen=True)
class Gift:
    key: str
    setting: str  # ключ настройки с адресом
    icon: str
    title: str  # текст ссылки
    about: str  # одна строка под ссылкой в «🎁 Подарки»


GIFTS: dict[str, Gift] = {g.key: g for g in (
    Gift("calc", "gift_url_calc", "🧮", "Разбор РГР: системы уравнений, вариант 14",
         "Обратная матрица, формулы Крамера и метод Гаусса — по шагам, плюс чистовик одним листом."),
    Gift("nachertalka", "gift_url_nach", "📐", "Тренажёр по начерталке: лекции 1–5, практика 5",
         "Понять, а не зазубрить: положения прямой, метод прямоугольного треугольника, тренировки."),
    Gift("matrices", "gift_url_matrices", "🔢", "Школа по матрицам: определители и системы",
         "От «что такое матрица» до обратной матрицы, Крамера, Гаусса и ранга — с тренажёром и прогрессом."),
)}

def gift_url(s, key: str) -> str:  # noqa: ANN001 — s: RuntimeSettings
    """Текущая ссылка подарка; пусто — подарок скрыт."""
    return str(s.get(GIFTS[key].setting) or "").strip()


def active(s) -> list[tuple[Gift, str]]:  # noqa: ANN001
    return [(g, url) for g in GIFTS.values() if (url := gift_url(s, g.key))]


def link_html(gift: Gift, url: str) -> str:
    return f'{gift.icon} <a href="{html.escape(url, quote=True)}">{html.escape(gift.title)}</a>'


def welcome_block(s) -> str:  # noqa: ANN001
    """Для подстановки {gifts} в приветствие: заголовок и ссылки строками. Пусто, если подарков нет."""
    items = active(s)
    if not items:
        return ""
    return "\n".join([texts.GIFTS_WELCOME_TITLE, *(link_html(g, url) for g, url in items)])


def menu_text(s) -> str:  # noqa: ANN001
    """Текст «🎁 Подарки»: ссылка + строка о том, что внутри."""
    items = active(s)
    if not items:
        return texts.GIFTS_EMPTY
    return "\n\n".join([texts.GIFTS_TITLE, *(f"{link_html(g, url)}\n{html.escape(g.about)}" for g, url in items)])


async def link_by_token(session: AsyncSession, token: str) -> GiftLink | None:
    """Персональные ссылки /g/{token} из старых кнопок Mini App (до v2.1) — теперь это редирект."""
    return await session.scalar(select(GiftLink).where(GiftLink.token == token))
