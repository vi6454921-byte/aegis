from sqlalchemy import func, select

from app.db.models import Payment, Transaction
from app.services import payments as pay
from app.services.ledger import lock_user
from app.services.maintenance import integrity_issues
from app.services.settings import load_settings, set_setting
from app.utils.timeutil import now_utc
from tests.helpers import invoiced_order, make_user


async def tx_count(session) -> int:  # noqa: ANN001
    return int(await session.scalar(select(func.count(Transaction.id))))


async def stars_settings(session):  # noqa: ANN001, ANN201
    await set_setting(session, "payments_mode", "stars")
    await session.commit()
    return await load_settings(session)


async def test_double_confirm_is_idempotent(session, s) -> None:
    user = await make_user(session, 4001)
    order = await invoiced_order(session, s, user.id)
    payment = await pay.start_order_payment(session, order.id, user.id, s, now_utc())
    first = await pay.confirm_payment(session, payment.id, s, now_utc())
    assert first.status == "order_paid"
    n = await tx_count(session)
    again = await pay.confirm_payment(session, payment.id, s, now_utc())
    assert again.status == "duplicate"
    assert await tx_count(session) == n
    user = await lock_user(session, user.id)
    assert user.balance_rub == 0  # пришло 200 ₽ и сразу ушло на заказ
    assert await integrity_issues(session) == []


async def test_stars_charge_id_reuse_rejected(session) -> None:
    s = await stars_settings(session)
    user = await make_user(session, 4002)
    o1 = await invoiced_order(session, s, user.id)
    p1 = await pay.start_order_payment(session, o1.id, user.id, s, now_utc())
    assert p1.provider == "stars" and p1.amount_stars == 115  # ceil(200 / 1,75)
    assert (await pay.confirm_payment(session, p1.id, s, now_utc(), charge_id="CH-1")).status == "order_paid"
    o2 = await invoiced_order(session, s, user.id)
    p2 = await pay.start_order_payment(session, o2.id, user.id, s, now_utc())
    assert (await pay.confirm_payment(session, p2.id, s, now_utc(), charge_id="CH-1")).status == "duplicate"
    assert (await session.get(Payment, p2.id, populate_existing=True)).status == "pending"
    assert await integrity_issues(session) == []


async def test_pre_checkout_checks(session) -> None:
    s = await stars_settings(session)
    user = await make_user(session, 4003)
    other = await make_user(session, 4004)
    order = await invoiced_order(session, s, user.id)
    p = await pay.start_order_payment(session, order.id, user.id, s, now_utc())
    assert await pay.check_pre_checkout(session, p.payload, p.amount_stars, "XTR", user.id) is None
    assert await pay.check_pre_checkout(session, "nope", p.amount_stars, "XTR", user.id) == "stale"
    assert await pay.check_pre_checkout(session, p.payload, p.amount_stars, "XTR", other.id) == "user"
    assert await pay.check_pre_checkout(session, p.payload, p.amount_stars + 1, "XTR", user.id) == "amount"
    assert await pay.check_pre_checkout(session, p.payload, p.amount_stars, "USD", user.id) == "amount"
    # новый счёт отменяет старый — старый payload больше не принимается
    p_new = await pay.start_order_payment(session, order.id, user.id, s, now_utc())
    assert await pay.check_pre_checkout(session, p.payload, p.amount_stars, "XTR", user.id) == "stale"
    assert await pay.check_pre_checkout(session, p_new.payload, p_new.amount_stars, "XTR", user.id) is None
    user.is_banned = True  # забаненному оплату не принимаем
    await session.commit()
    assert await pay.check_pre_checkout(session, p_new.payload, p_new.amount_stars, "XTR", user.id) == "banned"


async def test_cancelled_invoice_paid_anyway_goes_to_balance(session) -> None:
    s = await stars_settings(session)
    user = await make_user(session, 4005)
    order = await invoiced_order(session, s, user.id)
    old = await pay.start_order_payment(session, order.id, user.id, s, now_utc())
    new = await pay.start_order_payment(session, order.id, user.id, s, now_utc())  # old → cancelled
    assert (await pay.confirm_payment(session, new.id, s, now_utc(), charge_id="CH-A")).status == "order_paid"
    # Telegram всё-таки провёл старый счёт: деньги не теряем — на баланс
    res = await pay.confirm_payment(session, old.id, s, now_utc(), charge_id="CH-B")
    assert res.status == "credited_to_balance"
    user = await lock_user(session, user.id)
    assert user.balance_rub == 20000
    assert await integrity_issues(session) == []


async def test_topup_and_pay_from_balance(session, s) -> None:
    user = await make_user(session, 4006)
    top = await pay.start_topup(session, user.id, 50000, s, now_utc())
    assert (await pay.confirm_payment(session, top.id, s, now_utc())).status == "topup_ok"
    assert (await pay.confirm_payment(session, top.id, s, now_utc())).status == "duplicate"
    order = await invoiced_order(session, s, user.id)
    p = await pay.start_order_payment(session, order.id, user.id, s, now_utc())
    assert p.provider == "internal" and p.amount_rub == 0 and p.plan_balance == 20000
    assert (await pay.confirm_payment(session, p.id, s, now_utc())).status == "order_paid"
    user = await lock_user(session, user.id)
    assert user.balance_rub == 30000
    assert await integrity_issues(session) == []
