from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from app.config import Config


class IsAdmin(BaseFilter):
    """Доступ к админке: уровень админа ≥ level (1 — оператор, 2 — старший; владельцы всегда 2)."""

    def __init__(self, level: int = 1) -> None:
        self.level = level

    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return bool(event.from_user) and config.admin_level(event.from_user.id) >= self.level


class IsBanned(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, is_banned: bool = False) -> bool:
        return is_banned


class IsLevel(BaseFilter):
    """Ровно этот уровень админа — например, отказ «Недостаточно прав» админу 1 уровня на команды 2 уровня."""

    def __init__(self, level: int) -> None:
        self.level = level

    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return bool(event.from_user) and config.admin_level(event.from_user.id) == self.level
