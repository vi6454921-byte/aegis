from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import actionlog
from app.db.models import AdminLog


def log_admin(session: AsyncSession, admin_id: int | None, action: str, target_type: str | None = None,
              target_id: Any = None, **details: Any) -> None:
    """Пишет действие админа в admin_log (commit вместе с самим действием) и строку в журнал действий."""
    if admin_id is None:
        return
    actionlog.admin_action(admin_id, action, target_type, target_id, details)
    session.add(AdminLog(admin_id=admin_id, action=action, target_type=target_type,
                         target_id=None if target_id is None else str(target_id),
                         details=json.dumps(details, ensure_ascii=False, default=str) if details else None))
