from aiogram.fsm.state import State, StatesGroup


class OrderSG(StatesGroup):
    subject = State()
    photos = State()
    count = State()
    variant = State()
    deadline = State()
    deadline_pick = State()
    lecture = State()
    practice = State()
    comment = State()
    summary = State()
    terms = State()
    any_task = State()  # v2.4: «📎 Скинуть любую задачу»: фото, файлы и текст, потом «✅ Готово»


class DisputeSG(StatesGroup):
    screenshot = State()


class SupportSG(StatesGroup):
    message = State()


class AppealSG(StatesGroup):
    message = State()


class PaySG(StatesGroup):
    receipt = State()


class PromoSG(StatesGroup):
    """Ждём скрин или ссылку по заданию. Уход в другой раздел тихо отменяет ожидание (UserMiddleware)."""
    profile_shot = State()
    post_url = State()
    post_shot = State()
    views_shot = State()
    views_count = State()


class AdminSG(StatesGroup):
    user_search = State()
    adjust_amount = State()
    adjust_comment = State()
    message_user = State()
    order_price = State()
    order_count = State()
    order_reject = State()
    order_quote = State()  # v2.4: цена «📎 Любой задачи» в рублях
    upload_solution = State()
    setting_value = State()
    broadcast_message = State()
    ticket_reply = State()
    order_message = State()
    ban_reason = State()
    staff_add = State()
    # v2.2
    find_user = State()  # поиск по @username / ID для экранов 2 уровня (что дальше - в data["purpose"])
    give_amount = State()
    give_comment = State()
    vip_number = State()
    ban_custom = State()
    task_reason = State()
    views_number = State()
    post_adjust = State()


class PromoCodeSG(StatesGroup):
    """v2.3: ждём промокод на безлимит."""
    code = State()


class AdminPromoSG(StatesGroup):
    """v2.3: 🎟 Промокоды (админ 2 уровня)."""
    find_code = State()
    gen_count = State()
    user = State()  # @username / ID для «Выдать/Снять безлимит» (что дальше - в data["what"])


class DesignSG(StatesGroup):
    """v2.4: «🎨 Файлы и дизайн». Выбор хранится в данных состояния (data["dz"]), заказ создаётся на «Оплатить»."""
    kind = State()
    style = State()
    fmt = State()
    lang = State()
    qty = State()
    qty_input = State()
    content = State()
    summary = State()
    terms = State()
    revision = State()  # «🔁 Нужны правки»: сообщения клиента уходят исполнителю заказа
