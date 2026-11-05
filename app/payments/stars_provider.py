from __future__ import annotations

from aiogram import Bot
from aiogram.types import LabeledPrice

from app.db.models import Payment
from app.payments.base import PaymentProvider


class StarsPaymentProvider(PaymentProvider):
    """Telegram Stars: currency XTR, provider_token пустой, возврат — refundStarPayment."""

    name = "stars"

    async def create_invoice(self, bot: Bot, payment: Payment, title: str, description: str) -> None:
        await bot.send_invoice(
            chat_id=payment.user_id,
            title=title[:32],
            description=description[:255],
            payload=payment.payload,
            currency="XTR",
            prices=[LabeledPrice(label=title[:32], amount=int(payment.amount_stars or 0))],
            provider_token="",
        )

    async def refund(self, bot: Bot, payment: Payment) -> bool:
        return bool(await bot.refund_star_payment(user_id=payment.user_id,
                                                  telegram_payment_charge_id=payment.telegram_payment_charge_id))
