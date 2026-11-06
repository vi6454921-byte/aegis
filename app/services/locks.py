"""Один процесс бота - один замок на денежные и «местовые» операции.

Все изменения балансов, оплат и мест проходят под этим замком, а в БД - атомарно (одна транзакция). Для PostgreSQL дополнительно берём SELECT ... FOR UPDATE.
"""
from __future__ import annotations

import asyncio

_locks: dict[int, asyncio.Lock] = {}


def app_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _locks.get(id(loop))
    if lock is None:
        lock = _locks[id(loop)] = asyncio.Lock()
    return lock
