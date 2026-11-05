from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, User
from app.services import orders as svc
from app.services.users import register_user
from app.utils.timeutil import now_utc


def tg(uid: int, name: str = "Тест") -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=f"user{uid}", first_name=name, last_name=None)


async def make_user(session: AsyncSession, uid: int, payload: str | None = None, now: datetime | None = None) -> User:
    return (await register_user(session, tg(uid), payload, now or now_utc())).user


async def make_order(session: AsyncSession, s, user_id: int, *, subject: str = "nach", tasks: int = 1,  # noqa: ANN001
                     deadline_in: timedelta = timedelta(days=4), lecture: bool = False, practice: bool = False,
                     now: datetime | None = None) -> Order:
    now = now or now_utc()
    order = await svc.create_draft(session, user_id, subject, now)
    await svc.add_file(session, order.id, "condition", file_type="photo", tg_file_id="cond")
    if lecture:
        await svc.add_file(session, order.id, "lecture", file_type="photo", tg_file_id="lec")
    if practice:
        await svc.add_file(session, order.id, "practice", file_type="photo", tg_file_id="prac")
    order.task_count = tasks
    order.deadline_at = now + deadline_in
    await session.commit()
    return await svc.submit_order(session, order.id, s, now)


async def invoiced_order(session: AsyncSession, s, user_id: int, admin_id: int = 1001, **kw) -> Order:  # noqa: ANN001
    order = await make_order(session, s, user_id, **kw)
    for which in ("lecture", "practice"):
        if getattr(order, f"{which}_status") == "pending":
            await svc.set_note_status(session, order.id, which, True, admin_id)
    return await svc.issue_invoice(session, order.id, s, now_utc(), admin_id)
