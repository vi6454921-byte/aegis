"""FSM-хранилище aiogram в БД: состояния диалогов переживают рестарт."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import FSMRecord
from app.utils.timeutil import now_utc


def _key(key: StorageKey) -> str:
    return ":".join(str(p) for p in (key.bot_id, key.chat_id, key.user_id, key.thread_id or "",
                                      key.business_connection_id or "", key.destiny))


class DBStorage(BaseStorage):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]):
        self.sessionmaker = sessionmaker

    async def _upsert(self, key: StorageKey, **values: Any) -> None:
        async with self.sessionmaker() as session:
            row = await session.get(FSMRecord, _key(key))
            if row is None:
                row = FSMRecord(key=_key(key), state=None, data="{}")
                session.add(row)
            for k, v in values.items():
                setattr(row, k, v)
            row.updated_at = now_utc()
            await session.commit()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await self._upsert(key, state=value)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self.sessionmaker() as session:
            row = await session.get(FSMRecord, _key(key))
            return row.state if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self._upsert(key, data=json.dumps(dict(data), ensure_ascii=False, default=str))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self.sessionmaker() as session:
            row = await session.get(FSMRecord, _key(key))
            if row is None or not row.data:
                return {}
            try:
                return json.loads(row.data)
            except ValueError:
                return {}

    async def close(self) -> None:
        return None
