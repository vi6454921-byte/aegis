"""v2.4: «📎 Любая задача» (оценка цены админом), цены без наценок, тексты без тире

Статусы заказа (колонка orders.status, строка): добавлены
- estimate: «⏳ Ждёт оценки», клиент прислал любую задачу, админ ещё не назвал цену;
- quoted: «💰 Оценено», админ назвал цену, клиент решает: оплатить или отказаться.
В orders новые поля: quoted_at (когда назвали цену) и quoted_by (кто из админов).

Данные:
- цены: начерталка 200 ₽ и расчётка 400 ₽ за задачу, без скидок за конспекты и без наценки за срочность
  (строки price_nach, price_calc, notes_discount_* из settings убираем, действуют новые значения по умолчанию);
  уже созданные заказы не пересчитываются;
- тексты приветствия, условий, про вывод и шаблон поста: если админ их не менял (совпадают с v2.3),
  убираем из settings, чтобы действовали новые тексты v2.4.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 10:00:00.000000
"""
import json

import sqlalchemy as sa
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

PRICE_KEYS = ('price_nach', 'price_calc', 'notes_discount_nach', 'notes_discount_calc')
# Тексты по умолчанию из v2.3. Совпадает с сохранённым значением: админ его не менял, берём новый текст.
OLD_DEFAULTS = {
    'welcome_text': 'Привет! Это Аегис 🛡 Мы разбираем задачи по начерталке и вышмату: 3D-модель, полное решение и алгоритм.\n✅ {stats}. Не зачли \u2014 переделаем или вернём деньги.\n\n{gifts}',
    'terms_text': '<b>Условия работы Аегис</b>\n\n1. Аегис делает подробный разбор задачи: 3D-модель, решение и алгоритм. Это учебный материал \u2014 разбирайся по нему и оформляй работу сам.\n2. Цена считается автоматически: цена за задачу × количество задач × срочность × (1 \u2212 скидки). Итог виден до отправки заказа, счёт приходит после того, как мы проверим условие.\n3. Срочность считается по времени до дедлайна в момент отправки заказа. Актуальные множители \u2014 в «❓ Помощь» → «⏰ Сроки и цены».\n4. Скидки за конспекты лекции и практики действуют после проверки: фото читаемые и по теме ДЗ.\n5. Оплата: сначала бонусы (не больше доли цены, указанной в «💰 Баланс»), потом рублёвый баланс, остаток \u2014 счётом.\n6. Гарантия: если работу не зачли, пришли скрин замечания \u2014 переделаем или вернём деньги. Возврат \u2014 на баланс бота или, если платил звёздами, звёздами.\n7. Рублёвый баланс не сгорает, им можно оплатить заказ целиком. Вывод денег с баланса сейчас в разработке. Бонусы живут ограниченный срок, не выводятся и тратятся только на заказы.\n8. Бонус за подписку на канал начисляется один раз на аккаунт. Если отписаться в течение срока удержания \u2014 бонус списывается и повторно не начисляется.\n9. Работа начинается после оплаты. Сроки \u2014 до выбранного тобой дедлайна (время самарское).\n10. Вопросы \u2014 /support, вопросы по оплате \u2014 /paysupport.',
    'withdraw_note': '💸 Деньги на балансе можно будет вывести \u2014 функция вывода сейчас в разработке. Пока их можно потратить на решение домашних заданий в боте.',
    'post_template': '🛡 Нашёл бота, который разбирает задачи по начерталке и вышмату: 3D-модель, полное решение и алгоритм. Не зачли \u2014 переделают или вернут деньги.\nПопробуй: {link}',
}


def upgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('quoted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('quoted_by', sa.BigInteger(), nullable=True))

    conn = op.get_bind()
    settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))
    conn.execute(settings.delete().where(settings.c.key.in_(PRICE_KEYS)))
    rows = conn.execute(sa.select(settings.c.key, settings.c.value)
                        .where(settings.c.key.in_(list(OLD_DEFAULTS)))).all()
    for key, value in rows:
        try:
            current = json.loads(value)
        except ValueError:
            continue
        if current == OLD_DEFAULTS[key]:
            conn.execute(settings.delete().where(settings.c.key == key))


def downgrade() -> None:
    orders = sa.table("orders", sa.column("status", sa.String))
    # в v2.3 таких статусов нет: заявки на оценку закрываем как отменённые
    op.get_bind().execute(orders.update().where(orders.c.status.in_(("estimate", "quoted")))
                          .values(status="cancelled"))
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('quoted_by')
        batch_op.drop_column('quoted_at')
