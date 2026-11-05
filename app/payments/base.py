"""Интерфейс платёжного провайдера. Внутренний учёт — в рублях; провайдер — только способ оплаты."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment
from app.services import payments as payments_service


class PaymentProvider(ABC):
    name: str = ""
    is_test: bool = False

    @abstractmethod
    async def create_invoice(self, bot: Bot, payment: Payment, title: str, description: str) -> None:
        """Отправить пользователю счёт."""

    async def handle_success(self, session: AsyncSession, payment: Payment, s, now: datetime,  # noqa: ANN001
                             charge_id: str, provider_charge_id: str | None = None
                             ) -> payments_service.ConfirmResult:
        """Общий для всех провайдеров код зачисления."""
        return await payments_service.confirm_payment(session, payment.id, s, now, charge_id=charge_id,
                                                      provider_charge_id=provider_charge_id)

    @abstractmethod
    async def refund(self, bot: Bot, payment: Payment) -> bool:
        """Вернуть деньги через провайдера."""

# Третий провайдер (сервис для самозанятых с webhook и чеками) подключается так:
# 1) класс-наследник PaymentProvider с create_invoice (ссылка на оплату) и refund;
# 2) aiohttp-роут для webhook, который находит Payment по provider_payment_id и вызывает handle_success;
# 3) заполнение payments.receipt_url / receipt_status из ответа сервиса.
