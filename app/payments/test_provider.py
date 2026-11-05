from __future__ import annotations

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app import texts
from app.bot.callbacks import PayCb
from app.db.models import Payment
from app.payments.base import PaymentProvider
from app.utils.money import fmt_rub


class TestPaymentProvider(PaymentProvider):
    """Воображаемые деньги: счёт как настоящий, кнопка «Оплатить (тест)» проводит платёж тем же кодом."""

    name = "test"
    is_test = True

    async def create_invoice(self, bot: Bot, payment: Payment, title: str, description: str) -> None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.PAY_TEST_BTN, callback_data=PayCb(action="test", id=payment.id).pack())],
            [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=PayCb(action="drop", id=payment.id).pack())],
        ])
        await bot.send_message(payment.user_id, texts.TEST_INVOICE.format(
            pid=payment.id, title=title, description=description, amount=fmt_rub(payment.amount_rub)),
            reply_markup=kb)

    async def refund(self, bot: Bot, payment: Payment) -> bool:
        return True
