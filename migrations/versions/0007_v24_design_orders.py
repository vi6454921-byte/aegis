"""v2.4: «🎨 Файлы и дизайн»: презентации, PDF, картинки, баннеры и QR-коды по фиксированной цене

Заказы раздела: orders.subject = 'design', количество в task_count. Новые поля в orders:
- design_kind: pres | pdf | img | banner | qr;
- design_style: оформление презентации, custom (свой дизайн) | ssau (шаблон Самарского университета);
- design_format и design_lang: формат (16:9 | 4:3) и язык (ru | en) шаблона университета.
Старые заказы не меняются.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08 10:00:00.000000
"""
import sqlalchemy as sa
from alembic import op


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('design_kind', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('design_style', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('design_format', sa.String(length=8), nullable=True))
        batch_op.add_column(sa.Column('design_lang', sa.String(length=8), nullable=True))


def downgrade() -> None:
    orders = sa.table("orders", sa.column("subject", sa.String), sa.column("status", sa.String))
    # в версиях до этой раздела нет: неоплаченные заказы раздела закрываем как отменённые
    # (оплаченные остаются как есть, их админ доводит вручную)
    op.get_bind().execute(orders.update().where(orders.c.subject == "design", orders.c.status == "awaiting_payment")
                          .values(status="cancelled"))
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('design_lang')
        batch_op.drop_column('design_format')
        batch_op.drop_column('design_style')
        batch_op.drop_column('design_kind')
