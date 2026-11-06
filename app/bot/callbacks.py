from aiogram.filters.callback_data import CallbackData


class MenuCb(CallbackData, prefix="m"):
    action: str


class GiftCb(CallbackData, prefix="g"):
    """Кнопки подарков из старых сообщений (до v2.1). Новые не создаются - подарки теперь ссылки в тексте."""
    gift: str


class GuideCb(CallbackData, prefix="t"):
    page: int = 1
    a: str = "go"  # go | done


class SupCb(CallbackData, prefix="h"):
    """Клиент: ответить поддержке (id обращения; 0 - новое) или написать по заказу."""
    action: str  # reply | order
    id: int = 0


class WizCb(CallbackData, prefix="w"):
    action: str
    value: str = ""


class MyOrderCb(CallbackData, prefix="o"):
    action: str
    id: int = 0
    page: int = 0


class PayCb(CallbackData, prefix="p"):
    action: str  # order | balance | test | drop | topup | claim | noreceipt | claim_cancel
    id: int = 0


class AdminCb(CallbackData, prefix="a"):
    s: str  # раздел
    a: str = ""  # действие
    id: int = 0
    p: int = 0
    v: str = ""


class PromoCb(CallbackData, prefix="pr"):
    """Задания v2.2 у пользователя: «📌 Ссылка в профиле», «📣 Пост о нас», «📣 Мои посты»."""
    action: str  # profile | profile_send | post | plat | posts | pv | views | cancel
    id: int = 0
    v: str = ""


class StrayCb(CallbackData, prefix="sy"):
    """v2.3: «Это задание?» - сообщение вне оформления заказа и поддержки."""
    action: str  # order | support
    id: int = 0


class CodeCb(CallbackData, prefix="pc"):
    """v2.3: пользователь - ввести промокод на безлимит."""
    action: str  # enter | cancel


class DesignCb(CallbackData, prefix="dz"):
    """v2.4: «🎨 Файлы и дизайн»."""
    a: str  # open | kind | style | fmt | lang | qty | done | clear | back | pay | edit | cancel | terms | accept | redo | redo_end
    v: str = ""
    id: int = 0
