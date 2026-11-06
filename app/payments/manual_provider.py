from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import texts
from app.bot.callbacks import PayCb
from app.db.models import Payment
from app.payments.base import PaymentProvider
from app.utils.money import fmt_rub


class ManualPaymentProvider(PaymentProvider):
    """Перевод по реквизитам (карта/СБП): клиент присылает чек, админ 1 или 2 уровня подтверждает поступление.

    Реквизиты - настройка «manual_pay_details» («⚙️ Настройки» → «💳 Оплата»). Возврат - только на баланс бота.
    """

    name = "manual"
    details: str = ""

    def __init__(self, details: str = "") -> None:
        self.details = details

    async def create_invoice(self, bot: Bot, payment: Payment, title: str, description: str) -> None:
        details = self.details.strip()
        if not details:
            await bot.send_message(payment.user_id, texts.MANUAL_NOT_CONFIGURED)
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.MANUAL_CLAIM_BTN,
                                  callback_data=PayCb(action="claim", id=payment.id).pack())],
            [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=PayCb(action="drop", id=payment.id).pack())],
        ])
        await bot.send_message(payment.user_id, texts.MANUAL_INVOICE.format(
            pid=payment.id, title=title, amount=fmt_rub(payment.amount_rub), details=details), reply_markup=kb)

    async def refund(self, bot: Bot, payment: Payment) -> bool:
        return False  # деньги переводом возвращаются вручную; в боте - на баланс
