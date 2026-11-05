"""v2.1: подарки — ссылки в тексте вместо HTML-файлов и кнопок

Схема БД не меняется. Если админ не менял приветствие v2 (там «кнопки ниже»), убираем его из settings —
подхватится новое, со ссылками на подарки ({gifts}). Изменённое админом приветствие остаётся как было:
ссылки на подарки бот допишет в конце сам.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01 10:00:00.000000
"""
import json

import sqlalchemy as sa
from alembic import op


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


OLD_DEFAULTS = {
    "welcome_text": "Привет! Это Аегис 🛡 Мы разбираем задачи по начерталке и вышмату: 3D-модель, полное решение и алгоритм.\n🎁 Тебе два подарка — тренажёр по начерталке и школа по матрицам (кнопки ниже).\n✅ {stats}. Не зачли — переделаем или вернём деньги.",
}


def upgrade() -> None:
    conn = op.get_bind()
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))
    rows = conn.execute(sa.select(settings.c.key, settings.c.value)
                        .where(settings.c.key.in_(list(OLD_DEFAULTS)))).all()
    for key, raw in rows:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if value == OLD_DEFAULTS[key]:
            conn.execute(settings.delete().where(settings.c.key == key))


def downgrade() -> None:
    pass  # удалённая строка = значение по умолчанию; возвращать нечего
