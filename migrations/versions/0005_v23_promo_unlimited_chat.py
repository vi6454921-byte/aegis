"""v2.3: безлимит по промокодам, черновики без потерь, «Это задание?», чат админа с клиентом

Новые таблицы:
- promo_codes — одноразовые промокоды на безлимит (forever — навсегда, days — на N дней с активации);
  сразу добавляются 45 кодов (35 навсегда и 10 на 30 дней) — те же, что в promo_codes.txt;
- promo_attempts — попытки ввода промокода (защита от перебора: 5 ошибок в час);
- stray_messages — фото/файлы/текст вне оформления и поддержки, пока клиент не выбрал «заказ» или «поддержка»;
- admin_chats — режим чата админа с клиентом по заказу/обращению (переживает перезапуск).
В users — поля безлимита, в orders — флаг безлимитного заказа и отметка о предупреждении по черновику.
Старые данные не меняются: всё только добавляется.

ВНИМАНИЕ: коды промокодов записаны в этом файле — храните репозиторий закрытым.
Если коды утекли — отзовите их в боте (🔐 Управление → 🎟 Промокоды) и выпустите новые.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04 10:00:00.000000
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

FOREVER_CODES = (
    'AEGIS-RM4Q-9VU6', 'AEGIS-3SPG-T96J', 'AEGIS-8VYH-XCRQ', 'AEGIS-ZTHV-U6ZB', 'AEGIS-9UTC-BMZE',
    'AEGIS-FNY3-8KYD', 'AEGIS-788E-B7T6', 'AEGIS-MFKB-BVA4', 'AEGIS-HBYF-HDAT', 'AEGIS-86AP-B9HC',
    'AEGIS-8FHS-Q5HM', 'AEGIS-XHMR-YD4S', 'AEGIS-Y3YV-SR9D', 'AEGIS-U27H-VPB5', 'AEGIS-829G-U92Y',
    'AEGIS-VATE-V6ND', 'AEGIS-ZRDH-NCE8', 'AEGIS-K8MB-PNKY', 'AEGIS-FFE9-ARFF', 'AEGIS-PD9D-QRFX',
    'AEGIS-FSPT-ANXK', 'AEGIS-WXZJ-2G2N', 'AEGIS-VJX6-N5XA', 'AEGIS-SDT4-9J7G', 'AEGIS-TF8E-UDVE',
    'AEGIS-V7Y4-U6SH', 'AEGIS-Z6MC-NT9K', 'AEGIS-GTQ8-VV9M', 'AEGIS-A3UC-U9GQ', 'AEGIS-F45H-KSFZ',
    'AEGIS-M9DD-3Q26', 'AEGIS-U3M4-GJZ7', 'AEGIS-EPNB-VTMX', 'AEGIS-F94J-PSBD', 'AEGIS-RKHE-HGMJ',
)

DAYS30_CODES = (
    'AEGIS-CRFQ-2AP5', 'AEGIS-AAH9-QEQE', 'AEGIS-RSDF-24TS', 'AEGIS-WUSV-TREY', 'AEGIS-YX39-WXC8',
    'AEGIS-WGJB-ZB66', 'AEGIS-7FQF-PC3Z', 'AEGIS-FXKC-WHAT', 'AEGIS-QJDN-WSQS', 'AEGIS-CCNS-HPND',
)


def upgrade() -> None:
    op.create_table('promo_codes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('days', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.BigInteger(), nullable=True),
    sa.Column('used_by', sa.BigInteger(), nullable=True),
    sa.Column('used_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_by', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promo_codes')),
    sa.UniqueConstraint('code', name=op.f('uq_promo_codes_code'))
    )
    with op.batch_alter_table('promo_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promo_codes_used_by'), ['used_by'], unique=False)

    op.create_table('promo_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=True),
    sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promo_attempts'))
    )
    with op.batch_alter_table('promo_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promo_attempts_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promo_attempts_created_at'), ['created_at'], unique=False)

    op.create_table('stray_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('media_group_id', sa.String(length=64), nullable=True),
    sa.Column('content_type', sa.String(length=16), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('file_id', sa.String(length=255), nullable=True),
    sa.Column('file_unique_id', sa.String(length=128), nullable=True),
    sa.Column('file_type', sa.String(length=16), nullable=True),
    sa.Column('file_name', sa.String(length=255), nullable=True),
    sa.Column('mime_type', sa.String(length=128), nullable=True),
    sa.Column('size', sa.Integer(), nullable=True),
    sa.Column('prompt_message_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('ticket_id', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_stray_messages'))
    )
    with op.batch_alter_table('stray_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_stray_messages_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_stray_messages_status'), ['status'], unique=False)

    op.create_table('admin_chats',
    sa.Column('admin_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('ticket_id', sa.Integer(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('last_out_at', sa.DateTime(), nullable=True),
    sa.Column('sent', sa.Integer(), nullable=False, server_default='0'),
    sa.PrimaryKeyConstraint('admin_id', name=op.f('pk_admin_chats'))
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unlimited_forever', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('unlimited_until', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('unlimited_since', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('unlimited_code_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('unlimited_by', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('unlimited_reminded_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('unlimited', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('draft_warned_at', sa.DateTime(), nullable=True))

    codes = sa.table('promo_codes',
                     sa.column('code', sa.String), sa.column('kind', sa.String),
                     sa.column('days', sa.Integer), sa.column('created_at', sa.DateTime))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [{'code': c, 'kind': 'forever', 'days': None, 'created_at': now} for c in FOREVER_CODES]
    rows += [{'code': c, 'kind': 'days', 'days': 30, 'created_at': now} for c in DAYS30_CODES]
    op.bulk_insert(codes, rows)


def downgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('draft_warned_at')
        batch_op.drop_column('unlimited')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('unlimited_reminded_at')
        batch_op.drop_column('unlimited_by')
        batch_op.drop_column('unlimited_code_id')
        batch_op.drop_column('unlimited_since')
        batch_op.drop_column('unlimited_until')
        batch_op.drop_column('unlimited_forever')

    op.drop_table('admin_chats')
    with op.batch_alter_table('stray_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_stray_messages_status'))
        batch_op.drop_index(batch_op.f('ix_stray_messages_user_id'))

    op.drop_table('stray_messages')
    with op.batch_alter_table('promo_attempts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promo_attempts_created_at'))
        batch_op.drop_index(batch_op.f('ix_promo_attempts_user_id'))

    op.drop_table('promo_attempts')
    with op.batch_alter_table('promo_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promo_codes_used_by'))

    op.drop_table('promo_codes')
