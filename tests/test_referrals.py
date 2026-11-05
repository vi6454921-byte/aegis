import pytest
from sqlalchemy import select

from app.db.models import Referral
from app.services import orders as svc
from app.services import payments as pay
from app.services.ledger import available_bonus, credit_bonus, debit_bonus, lock_user
from app.services.maintenance import integrity_issues
from app.services.settings import load_settings, set_setting
from app.utils.timeutil import now_utc
from tests.helpers import invoiced_order, make_order, make_user

ADMIN = 1001


@pytest.fixture
async def s(session):  # noqa: ANN001, ANN201 — бонус за друга с оплаченным заказом по умолчанию выключен (0)
    await set_setting(session, "referral_bonus", 5000)
    await session.commit()
    return await load_settings(session)


async def deliver(session, s, order_id: int):  # noqa: ANN001, ANN201
    now = now_utc()
    await svc.take_in_work(session, order_id, now, ADMIN)
    await svc.attach_solution(session, order_id, "index.html", [], now, ADMIN)
    return await svc.mark_done(session, order_id, s, now, ADMIN)


async def paid_by_money(session, s, user_id: int):  # noqa: ANN001, ANN201
    order = await invoiced_order(session, s, user_id)
    payment = await pay.start_order_payment(session, order.id, user_id, s, now_utc())
    res = await pay.confirm_payment(session, payment.id, s, now_utc())
    assert res.status == "order_paid"
    return res.order


async def test_referral_reward_once_after_delivery_and_revoke(session, s) -> None:
    ref = await make_user(session, 3001)
    friend = await make_user(session, 3002, payload=f"ref_{ref.ref_code}")
    assert friend.referrer_id == ref.id

    order = await paid_by_money(session, s, friend.id)
    assert await available_bonus(session, ref.id, now_utc()) == 0  # бонус только после выдачи
    _, reward = await deliver(session, s, order.id)
    assert reward is not None and reward.amount == 5000
    assert await available_bonus(session, ref.id, now_utc()) == 5000

    second = await paid_by_money(session, s, friend.id)
    _, reward2 = await deliver(session, s, second.id)
    assert reward2 is None  # только за первый оплаченный заказ
    assert await available_bonus(session, ref.id, now_utc()) == 5000

    # возврат НЕ того заказа, за который дали бонус, ничего не отзывает
    res = await pay.refund_order(session, second.id, "balance", s, now_utc(), ADMIN)
    assert res.revoke is None
    # возврат заказа с наградой — бонус отзывается
    res = await pay.refund_order(session, order.id, "balance", s, now_utc(), ADMIN)
    assert res.revoke is not None and res.revoke.revoked == 5000
    assert await available_bonus(session, ref.id, now_utc()) == 0
    referral = await session.scalar(select(Referral).where(Referral.referred_id == friend.id)
                                    .execution_options(populate_existing=True))
    assert referral.rewarded is False and referral.revoked_count == 1
    assert await integrity_issues(session) == []


async def test_revoke_never_goes_below_zero(session, s) -> None:
    ref = await make_user(session, 3011)
    friend = await make_user(session, 3012, payload=f"ref_{ref.ref_code}")
    order = await paid_by_money(session, s, friend.id)
    await deliver(session, s, order.id)
    user = await lock_user(session, ref.id)
    await debit_bonus(session, user, 3000, kind="order_pay", now=now_utc())  # друг-реферер потратил 30 ₽
    await session.commit()
    res = await pay.refund_order(session, order.id, "balance", s, now_utc(), ADMIN)
    assert res.revoke.revoked == 2000  # отозвали только остаток
    user = await lock_user(session, ref.id)
    assert user.balance_bonus == 0
    assert await integrity_issues(session) == []


async def test_reward_needs_real_money(session, s) -> None:
    # половина бонусами, половина деньгами — награда положена
    ref = await make_user(session, 3021)
    friend = await make_user(session, 3022, payload=f"ref_{ref.ref_code}")
    user = await lock_user(session, friend.id)
    await credit_bonus(session, user, 100000, source="admin", kind="admin_credit", ttl_days=30, now=now_utc())
    await session.commit()
    order = await invoiced_order(session, s, friend.id)
    payment = await pay.start_order_payment(session, order.id, friend.id, s, now_utc())
    assert payment.plan_bonus == 10000 and payment.amount_rub == 10000
    assert (await pay.confirm_payment(session, payment.id, s, now_utc())).status == "order_paid"
    _, reward = await deliver(session, s, order.id)
    assert reward is not None and reward.amount == 5000

    # бесплатный заказ (цена 0) награду не даёт
    ref2 = await make_user(session, 3031)
    friend2 = await make_user(session, 3032, payload=f"ref_{ref2.ref_code}")
    free = await make_order(session, s, friend2.id)
    await svc.set_price_override(session, free.id, 0, ADMIN)
    free = await svc.issue_invoice(session, free.id, s, now_utc(), ADMIN)
    assert free.status == "paid" and free.price_final == 0
    _, reward = await deliver(session, s, free.id)
    assert reward is None
    assert await available_bonus(session, ref2.id, now_utc()) == 0
    assert await integrity_issues(session) == []


async def test_self_referral_ignored(session) -> None:
    user = await make_user(session, 3041)
    again = await make_user(session, 3041, payload=f"ref_{user.ref_code}")
    assert again.referrer_id is None
