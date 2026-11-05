from datetime import timedelta

from sqlalchemy import select

from app.db.models import BonusLot, Transaction
from app.services import payments as pay
from app.services.ledger import available_bonus, credit_bonus, debit_bonus, expire_due_lots, lock_user
from app.services.maintenance import integrity_issues
from app.utils.timeutil import now_utc
from tests.helpers import invoiced_order, make_user


async def test_bonus_spent_oldest_first(session) -> None:
    now = now_utc()
    user = await make_user(session, 2001)
    old = await credit_bonus(session, user, 3000, source="admin", kind="admin_credit", ttl_days=30,
                             now=now - timedelta(days=20))
    new = await credit_bonus(session, user, 5000, source="admin", kind="admin_credit", ttl_days=30, now=now)
    await session.commit()
    user = await lock_user(session, user.id)
    taken = await debit_bonus(session, user, 4000, kind="order_pay", now=now)
    await session.commit()
    await session.refresh(old)
    await session.refresh(new)
    assert taken == 4000
    assert old.remaining == 0 and new.remaining == 4000
    assert user.balance_bonus == 4000
    assert await integrity_issues(session) == []


async def test_expired_lots_burn(session) -> None:
    now = now_utc()
    user = await make_user(session, 2002)
    burned_lot = await credit_bonus(session, user, 5000, source="referral", kind="referral", ttl_days=30,
                                    now=now - timedelta(days=31))
    alive = await credit_bonus(session, user, 2000, source="admin", kind="admin_credit", ttl_days=30, now=now)
    await session.commit()
    assert await available_bonus(session, user.id, now) == 2000  # просроченная партия уже не доступна

    burned = await expire_due_lots(session, now)
    await session.commit()
    assert burned == {user.id: 5000}
    user = await lock_user(session, user.id)
    assert user.balance_bonus == 2000
    await session.refresh(burned_lot)
    assert burned_lot.expired and burned_lot.remaining == 0
    assert (await session.get(BonusLot, alive.id)).remaining == 2000
    kinds = [t.kind for t in (await session.execute(select(Transaction).where(Transaction.user_id == user.id)
                                                    .order_by(Transaction.id))).scalars()]
    assert kinds[-1] == "expire"
    assert await expire_due_lots(session, now) == {}  # повторный запуск ничего не делает
    assert await integrity_issues(session) == []


async def test_bonus_share_limited_to_50_percent(session, s) -> None:
    now = now_utc()
    user = await make_user(session, 2003)
    await credit_bonus(session, user, 15000, source="admin", kind="admin_credit", ttl_days=30, now=now)
    await session.commit()
    order = await invoiced_order(session, s, user.id, tasks=1)  # 200 ₽, скидок нет
    assert order.price_final == 20000
    payment = await pay.start_order_payment(session, order.id, user.id, s, now)
    assert payment.plan_bonus == 10000  # ровно половина цены, хотя бонусов 150 ₽
    assert payment.plan_balance == 0
    assert payment.amount_rub == 10000
    res = await pay.confirm_payment(session, payment.id, s, now)
    assert res.status == "order_paid"
    assert res.order.paid_bonus == 10000 and res.order.paid_invoice == 10000
    assert await available_bonus(session, user.id, now) == 5000
    assert await integrity_issues(session) == []
