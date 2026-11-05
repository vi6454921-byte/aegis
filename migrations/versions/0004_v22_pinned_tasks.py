"""v2.2: закреплённые админы 2 уровня, задания «Ссылка в профиле» и «Пост о нас»

Новые таблицы: pinned_admins (привязка @username → Telegram ID закреплённых админов),
profile_claims (заявки на задание «Ссылка в профиле»), promo_posts и promo_post_reports
(задание «Пост о нас» и отчёты о просмотрах). В admins добавлен флаг pinned.
Старые данные не меняются: всё только добавляется.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03 10:00:00.000000
"""
import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('pinned_admins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('bound_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_by', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pinned_admins')),
    sa.UniqueConstraint('username', name=op.f('uq_pinned_admins_username'))
    )
    with op.batch_alter_table('pinned_admins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pinned_admins_user_id'), ['user_id'], unique=False)

    op.create_table('profile_claims',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('link', sa.String(length=255), nullable=True),
    sa.Column('file_id', sa.String(length=255), nullable=True),
    sa.Column('file_type', sa.String(length=16), nullable=True),
    sa.Column('bio_check', sa.String(length=16), nullable=True),
    sa.Column('reward', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('reviewer_id', sa.BigInteger(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('reject_reason', sa.Text(), nullable=True),
    sa.Column('recheck_at', sa.DateTime(), nullable=True),
    sa.Column('rechecked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_amount', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_profile_claims_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_profile_claims'))
    )
    with op.batch_alter_table('profile_claims', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_profile_claims_recheck_at'), ['recheck_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_profile_claims_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_profile_claims_user_id'), ['user_id'], unique=False)

    op.create_table('promo_posts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('platform', sa.String(length=8), nullable=False),
    sa.Column('post_url', sa.String(length=512), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('base_reward_paid', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('verified_views', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('views_reward_paid', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('adjust_total', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('reject_reason', sa.Text(), nullable=True),
    sa.Column('reviewer_id', sa.BigInteger(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_by', sa.BigInteger(), nullable=True),
    sa.Column('last_report_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_update_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_promo_posts_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promo_posts')),
    sa.UniqueConstraint('post_url', name=op.f('uq_promo_posts_post_url'))
    )
    with op.batch_alter_table('promo_posts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promo_posts_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_promo_posts_user_id'), ['user_id'], unique=False)

    op.create_table('promo_post_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('post_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('file_id', sa.String(length=255), nullable=True),
    sa.Column('file_type', sa.String(length=16), nullable=True),
    sa.Column('claimed_views', sa.BigInteger(), nullable=True),
    sa.Column('verified_views', sa.BigInteger(), nullable=True),
    sa.Column('credited', sa.BigInteger(), nullable=False, server_default='0'),
    sa.Column('reviewer_id', sa.BigInteger(), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('reject_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['post_id'], ['promo_posts.id'], name=op.f('fk_promo_post_reports_post_id_promo_posts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promo_post_reports'))
    )
    with op.batch_alter_table('promo_post_reports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_promo_post_reports_post_id'), ['post_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_promo_post_reports_status'), ['status'], unique=False)

    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pinned', sa.Boolean(), nullable=False, server_default=sa.false()))



def downgrade() -> None:
    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.drop_column('pinned')

    with op.batch_alter_table('promo_post_reports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promo_post_reports_status'))
        batch_op.drop_index(batch_op.f('ix_promo_post_reports_post_id'))

    op.drop_table('promo_post_reports')
    with op.batch_alter_table('promo_posts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_promo_posts_user_id'))
        batch_op.drop_index(batch_op.f('ix_promo_posts_status'))

    op.drop_table('promo_posts')
    with op.batch_alter_table('profile_claims', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_profile_claims_user_id'))
        batch_op.drop_index(batch_op.f('ix_profile_claims_status'))
        batch_op.drop_index(batch_op.f('ix_profile_claims_recheck_at'))

    op.drop_table('profile_claims')
    with op.batch_alter_table('pinned_admins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pinned_admins_user_id'))

    op.drop_table('pinned_admins')
