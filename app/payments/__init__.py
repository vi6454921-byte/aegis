from __future__ import annotations

from app.payments.base import PaymentProvider


def get_provider(name: str) -> PaymentProvider:
    from app.payments.manual_provider import ManualPaymentProvider
    from app.payments.stars_provider import StarsPaymentProvider
    from app.payments.test_provider import TestPaymentProvider

    providers: dict[str, type[PaymentProvider]] = {"test": TestPaymentProvider, "stars": StarsPaymentProvider,
                                                   "manual": ManualPaymentProvider}
    if name not in providers:
        raise KeyError(f"Неизвестный платёжный провайдер: {name}")
    return providers[name]()
