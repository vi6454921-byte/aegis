"""/start, обучалка, помощь, подарки, баланс, «Пригласить друга», номер телефона, условия, фолбэк."""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import (
    KICKED,
    MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandObject,
    CommandStart,
    StateFilter,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot import guide
from app.bot.callbacks import AdminCb, CodeCb, GiftCb, GuideCb, MenuCb, PayCb, PromoCb
from app.bot.commands import ADMIN_COMMAND_NAMES, commands_text, refresh_commands
from app.bot.handlers.channel import channel_buttons, channel_promo
from app.bot import drafts
from app.bot.keyboards import Btn, cb, ik, main_menu, phone_kb, set_menu_admin
from app.bot.notify import safe_send
from app.bot.render import unlimited_status
from app.bot.ui import show
from app.config import Config
from app.db.models import Referral, Transaction, User
from app.services import channel as ch
from app.services import gifts, owner, roles, stats
from app.services.ledger import active_lots
from app.services.settings import RuntimeSettings
from app.services.users import display_name, paid_friends, referrals_of, short_name
from app.utils.money import fmt_rub, stars_for
from app.utils.timeutil import fmt_date, fmt_dt, now_utc

log = logging.getLogger(__name__)

menu_router = Router(name="menu")  # кнопки меню и команды - работают из любого состояния
router = Router(name="common")
fallback_router = Router(name="fallback")

M = texts.MAIN_MENU


_GIFTS_LINE = re.compile(r"^[ \t]*\{gifts\}[ \t]*(?:\r?\n|$)", re.MULTILINE)


def render_welcome(template: str, stats_line: str, name: str | None, s: RuntimeSettings) -> str:
    """Приветствие из настройки: {stats}, {name} и {gifts} - подарки ссылками прямо в тексте.
    Если {gifts} из текста убрали, ссылки всё равно добавляются в конце: кнопок подарков больше нет."""
    text = template.replace("{stats}", stats_line).replace("{name}", html.escape(name or ""))
    block = gifts.welcome_block(s)
    if "{gifts}" not in text:
        return f"{text.rstrip()}\n\n{block}" if block else text
    if not block:
        text = _GIFTS_LINE.sub("", text)
    return text.replace("{gifts}", block).rstrip()


def drop_button(markup: InlineKeyboardMarkup | None, data: str) -> InlineKeyboardMarkup | None:
    """Та же клавиатура без кнопки с callback_data == data."""
    if markup is None:
        return None
    rows = [[b for b in row if b.callback_data != data] for row in markup.inline_keyboard]
    rows = [r for r in rows if r]
    return ik(*rows) if rows else None


# ------------------------------------------------------------------ /start
async def accept_admin_invite(message: Message, session: AsyncSession, config: Config, bot: Bot, user: User,
                              token: str, level_before: int) -> bool:
    """/start adm_<код>: стать админом по ссылке-приглашению. True - экран показан, приветствие не нужно."""
    try:
        res = await roles.accept_invite(session, config, token, user.id, now_utc())
    except roles.RoleError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}")
        return False
    set_menu_admin(True)
    if level_before > 0 and level_before >= res.level:
        await message.answer(f"ℹ️ Ты уже админ ({roles.LEVELS[level_before]}). Админ-панель: /admin.",
                             reply_markup=main_menu())
        return True
    await refresh_commands(bot, config, user.id)
    await message.answer(texts.ADMIN_INVITE_OK.format(level=roles.LEVELS[res.level]), reply_markup=main_menu())
    if res.inviter_id != user.id:
        await safe_send(bot, res.inviter_id,
                        f"✅ {display_name(user)} принял приглашение: теперь админ ({roles.LEVELS[res.level]}).",
                        reply_markup=ik([cb("👮 Админы", AdminCb(s="st", a="list"))]))
    log.warning("новый админ по приглашению: %s (id %s), %s", user.first_name, user.id, roles.LEVELS[res.level])
    return True


@menu_router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession,
                    db_user: User, s: RuntimeSettings, config: Config, bot: Bot, admin_level: int,
                    is_new_user: bool, referrer: User | None) -> None:
    await state.clear()
    args = (command.args or "").strip()
    if args.startswith("adm_") and await accept_admin_invite(message, session, config, bot, db_user, args[4:],
                                                             admin_level):
        return
    done, pct = await stats.trust_numbers(session, s)
    await message.answer(render_welcome(s.welcome_text, stats.trust_line(done, pct), db_user.first_name, s))
    info = await drafts.refresh_menu(session, db_user.id)
    await message.answer(texts.MENU_HINT + (f"\n\n{texts.ADMIN_HINT}" if admin_level else ""),
                         reply_markup=main_menu())
    drafts.mark_shown()
    if info is not None:  # v2.3: незаконченный черновик - сразу предлагаем продолжить (или удалить)
        text, kb = drafts.screen(info)
        await message.answer(text, reply_markup=kb)
    if db_user.tutorial_done_at is None and admin_level == 0:
        await message.answer(guide.tutorial_page(1, s), reply_markup=guide_kb(1, s, db_user))
    if is_new_user and referrer is not None:
        await safe_send(bot, referrer.id, texts.REF_NEW_FRIEND if s.channel_ref_on else texts.REF_NEW_FRIEND_PAID)


# ------------------------------------------------------------------ обучалка
def guide_kb(page: int, s: RuntimeSettings, user: User) -> InlineKeyboardMarkup:
    n = guide.TUTORIAL_PAGES
    rows: list[list[InlineKeyboardButton]] = []
    if page == 2:
        rows.append([cb(M["order"], MenuCb(action="order"))])
    elif page == 3:
        rows.append([cb(M["explain"], MenuCb(action="explain"))])
    elif page == n:
        rows += channel_promo(user, s)[1]
    nav = []
    if page > 1:
        nav.append(cb(texts.GUIDE_PREV, GuideCb(page=page - 1)))
    nav.append(cb(texts.GUIDE_NEXT, GuideCb(page=page + 1)) if page < n else cb(texts.GUIDE_DONE, GuideCb(page=n, a="done")))
    rows.append(nav)
    if page < n:
        rows.append([cb(texts.GUIDE_SKIP, GuideCb(page=page, a="done"))])
    return ik(*rows)


@menu_router.message(Command("guide"))
async def cmd_guide(message: Message, state: FSMContext, db_user: User, s: RuntimeSettings) -> None:
    await state.clear()
    await message.answer(guide.tutorial_page(1, s), reply_markup=guide_kb(1, s, db_user))


@router.callback_query(MenuCb.filter(F.action == "guide"))
async def cb_guide(call: CallbackQuery, db_user: User, s: RuntimeSettings) -> None:
    await call.answer()
    await call.message.answer(guide.tutorial_page(1, s), reply_markup=guide_kb(1, s, db_user))


@router.callback_query(GuideCb.filter(F.a == "go"))
async def guide_page(call: CallbackQuery, callback_data: GuideCb, db_user: User, s: RuntimeSettings) -> None:
    page = min(max(callback_data.page, 1), guide.TUTORIAL_PAGES)
    await call.answer()
    await show(call, guide.tutorial_page(page, s), guide_kb(page, s, db_user))


@router.callback_query(GuideCb.filter(F.a == "done"))
async def guide_done(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    if db_user.tutorial_done_at is None:
        db_user.tutorial_done_at = now_utc()
        await session.commit()
    await call.answer()
    await show(call, texts.GUIDE_FINISHED, ik([cb(M["order"], MenuCb(action="order")),
                                               cb(M["explain"], MenuCb(action="explain"))]))


# ------------------------------------------------------------------ подарки
def gifts_screen(user: User, s: RuntimeSettings) -> tuple[str, InlineKeyboardMarkup | None]:
    """Подарки - ссылками в тексте; кнопки только у предложения подписаться на канал."""
    text = gifts.menu_text(s)
    promo_text, promo_rows = channel_promo(user, s)
    if promo_text:
        text += "\n\n" + promo_text
    return text, ik(*promo_rows) if promo_rows else None


@menu_router.message(F.text == M["gifts"])
async def menu_gifts(message: Message, state: FSMContext, db_user: User, s: RuntimeSettings) -> None:
    await state.clear()
    text, kb = gifts_screen(db_user, s)
    await message.answer(text, reply_markup=kb)


@router.callback_query(GiftCb.filter())
async def old_gift_button(call: CallbackQuery, db_user: User, s: RuntimeSettings) -> None:
    """Кнопки подарков из старых сообщений (до v2.1, присылали HTML-файл) - теперь присылаем ссылки."""
    await call.answer()
    text, kb = gifts_screen(db_user, s)
    await call.message.answer(text, reply_markup=kb)


# ------------------------------------------------------------------ баланс
def bonuses_exist(s: RuntimeSettings) -> bool:
    return bool(s.channel_bonus_on or s.channel_ref_on or int(s.referral_bonus) > 0)


async def balance_text(session: AsyncSession, user: User, s: RuntimeSettings) -> str:
    lines = [texts.BALANCE.format(rub=fmt_rub(user.balance_rub), bonus=fmt_rub(user.balance_bonus),
                                  share=s.bonus_max_share_pct)]
    for lot in await active_lots(session, user.id, now_utc()):
        lines.append(texts.BALANCE_LOT.format(amount=fmt_rub(lot.remaining), date=fmt_date(lot.expires_at)))
    if user.free_month_until and user.free_month_until > now_utc():
        lines.append(f"🎁 «Месяц бесплатно» до {fmt_date(user.free_month_until)}: осталось задач "
                     f"{user.free_month_tasks_left}")
    if user.is_priority:
        lines.append("⭐ Статус «Приоритет»: твои заказы выше в очереди")
    status = unlimited_status(user)
    if status:
        lines.insert(0, f"<b>{status}</b>: {texts.UNLIMITED_BALANCE_NOTE}\n")
    if bonuses_exist(s):
        lines.append("\n" + texts.BALANCE_HOW)
    elif tasks_exist(s):
        lines.append("\n" + texts.BALANCE_HOW_TASKS)
    lines.append("\n" + s.withdraw_note)
    return "\n".join(lines)


def tasks_exist(s: RuntimeSettings) -> bool:
    return bool(s.profile_task_enabled or s.post_task_enabled)


def balance_kb(user: User) -> InlineKeyboardMarkup:
    """v2.4: сетка 2×2. «💸 Вывести» пока показывает, что вывод скоро появится (и «← Назад» к балансу)."""
    return ik([cb(texts.BALANCE_TOPUP_BTN, MenuCb(action="topup")), cb(texts.BALANCE_HISTORY_BTN, MenuCb(action="history"))],
              [cb(texts.BALANCE_BONUSES_BTN, MenuCb(action="bonuses")),
               cb(texts.BALANCE_WITHDRAW_BTN, MenuCb(action="withdraw"))])


@router.callback_query(MenuCb.filter(F.action == "withdraw"))
async def cb_withdraw(call: CallbackQuery) -> None:
    """«💸 Вывести»: вывода пока нет. Экран меняется на месте, «← Назад» возвращает баланс."""
    await call.answer()
    await show(call, texts.WITHDRAW_SCREEN, ik([cb(texts.BTN_BACK_ARROW, MenuCb(action="balance"))]))


@router.callback_query(MenuCb.filter(F.action == "balance"))
async def cb_balance(call: CallbackQuery, session: AsyncSession, db_user: User, s: RuntimeSettings) -> None:
    """«← Назад» с экрана вывода: снова баланс в том же сообщении."""
    await call.answer()
    await show(call, await balance_text(session, db_user, s), balance_kb(db_user))


def task_rows(s: RuntimeSettings) -> list[list[InlineKeyboardButton]]:
    """Кнопки заданий v2.2 на экране «🎁 Бонусы и акции»."""
    first = []
    if s.profile_task_enabled:
        first.append(cb(texts.PROFILE_BTN, PromoCb(action="profile")))
    if s.post_task_enabled:
        first.append(cb(texts.POST_BTN, PromoCb(action="post")))
    return [r for r in (first, [cb(texts.MY_POSTS_BTN, PromoCb(action="posts"))]) if r]


def bonuses_screen(user: User, s: RuntimeSettings) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """«🎁 Бонусы и акции»: задания, подписка на канал и (v2.3) «🎟 Ввести промокод»."""
    text = guide.bonuses_text(s)
    status = unlimited_status(user)
    text += f"\n\n<b>{status}</b>" if status else f"\n\n{texts.PROMO_BONUS_LINE}"
    return text, task_rows(s) + channel_promo(user, s)[1] + [[cb(texts.PROMO_BTN, CodeCb(action="enter"))]]


@router.callback_query(MenuCb.filter(F.action == "bonuses"))
async def cb_bonuses(call: CallbackQuery, db_user: User, s: RuntimeSettings) -> None:
    """Из «💰 Баланс»: экран бонусов новым сообщением - баланс остаётся на месте."""
    await call.answer()
    text, rows = bonuses_screen(db_user, s)
    await call.message.answer(text, reply_markup=ik(*rows, [cb(texts.BTN_BACK, MenuCb(action="help"))]))


@menu_router.message(F.text == M["balance"])
async def menu_balance(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                       s: RuntimeSettings) -> None:
    await state.clear()
    await message.answer(await balance_text(session, db_user, s), reply_markup=balance_kb(db_user))


@router.callback_query(MenuCb.filter(F.action == "topup"))
async def topup_menu(call: CallbackQuery, s: RuntimeSettings) -> None:
    rows = []
    for amount in s.topup_packages:
        label = fmt_rub(int(amount))
        if s.payments_mode == "stars":
            label += f" · {stars_for(int(amount), s.rate)} ⭐"
        bonus = int(s.topup_bonus_map.get(str(amount), 0)) if s.topup_bonus_enabled else 0
        if bonus:
            label += texts.TOPUP_BONUS_NOTE.format(bonus=fmt_rub(bonus))
        rows.append([cb(label, PayCb(action="topup", id=int(amount)))])
    await call.answer()
    await call.message.answer(texts.TOPUP_TEXT, reply_markup=ik(*rows))


@router.callback_query(MenuCb.filter(F.action == "history"))
async def history(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    rows = (await session.execute(select(Transaction).where(Transaction.user_id == db_user.id)
                                  .order_by(Transaction.id.desc()).limit(15))).scalars().all()
    await call.answer()
    if not rows:
        await call.message.answer(texts.HISTORY_EMPTY)
        return
    lines = ["📜 <b>Последние операции</b>"]
    for tx in rows:
        acc = "₽" if tx.account == "rub" else "бонусы"
        sign = "+" if tx.amount > 0 else ""
        what = texts.TX_KINDS.get(tx.kind, tx.kind) + (f" #{tx.order_id}" if tx.order_id else "")
        lines.append(f"{fmt_dt(tx.created_at)} · {sign}{fmt_rub(tx.amount)} ({acc}) · {what}"
                     + (" 🧪" if tx.is_test else ""))
    await call.message.answer("\n".join(lines))


# ------------------------------------------------------------------ «👥 Пригласить друга»
def friend_status(ref: Referral, s: RuntimeSettings) -> str:
    parts = []
    if ref.sub_status == "rewarded":
        parts.append("🎁 подписался: бонус получен")
    elif ref.sub_status == "pending":
        parts.append(f"⏳ подписался: бонус после {fmt_dt(ref.sub_due_at)}" if ref.sub_due_at else "⏳ подписался")
    elif ref.sub_status == "cancelled":
        parts.append("✖️ бонус за подписку не начислен")
    elif s.channel_ref_on:
        parts.append("⏳ ещё не подписался на канал")
    if ref.rewarded:
        parts.append("✅ оплатил заказ")
    elif not parts:
        parts.append("⏳ ждём первый заказ")
    return " · ".join(parts)


async def referrer_status(bot: Bot, session: AsyncSession, s: RuntimeSettings, user: User) -> str | None:
    """Подписан ли сам пригласивший (кэш, при необходимости - живая проверка)."""
    status = user.channel_status
    if status != ch.MEMBER:
        fresh = await ch.fetch_status(bot, s.channel, user.id, now_utc())
        if fresh is not None and fresh != status:
            user.channel_status, user.channel_checked_at = fresh, now_utc()
            await session.commit()
        status = fresh or status
    return status


def channel_offer(s: RuntimeSettings) -> str:
    bonus, friend = int(s.channel_ref_bonus), int(s.channel_bonus) if s.channel_bonus_on else 0
    title = (f"🎁 <b>Вам обоим: по {fmt_rub(bonus)} бонусами:</b>" if friend == bonus
             else "🎁 <b>Бонусы вам обоим:</b>")
    hold = int(s.channel_hold_hours)
    channel = html.escape(s.channel)
    lines = [title, "1️⃣ Друг впервые заходит в бота по твоей ссылке.",
             f"2️⃣ Подписывается на наш канал {channel}" + (f" и сразу получает {fmt_rub(friend)}." if friend else ".")]
    when = f"Через {hold} ч" if hold else "Сразу"
    lines.append(f"3️⃣ {when} тебе: {fmt_rub(bonus)}, если друг всё ещё подписан, а ты тоже подписан на канал.")
    return "\n".join(lines)


@menu_router.message(F.text == M["invite"])
async def menu_invite(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                      s: RuntimeSettings, bot: Bot, bot_username: str) -> None:
    await state.clear()
    link = f"https://t.me/{bot_username}?start=ref_{db_user.ref_code}"
    n = await paid_friends(session, db_user.id)
    fm = int(s.free_month_threshold)
    lines = [texts.INVITE_TITLE, texts.INVITE_LINK.format(link=link), ""]
    rows: list[list[InlineKeyboardButton]] = []
    need_sub = False
    if s.channel_ref_on:
        lines.append(channel_offer(s))
        need_sub = await referrer_status(bot, session, s, db_user) != ch.MEMBER
    if int(s.referral_bonus) > 0:
        lines.append(texts.INVITE_PAID_BONUS.format(bonus=fmt_rub(int(s.referral_bonus))))
    lines += ["", texts.INVITE_MILESTONES.format(pr=s.priority_threshold, fm=fm, days=s.free_month_days,
                                                 tasks=s.free_month_tasks)]
    status = ""
    if db_user.is_priority:
        status = "\n⭐ У тебя статус «Приоритет»."
    elif n < int(s.priority_threshold):
        status = f"\nДо «Приоритета»: {n}/{s.priority_threshold}"
    lines.append(texts.INVITE_PROGRESS.format(n=min(n, fm), fm=fm, status=status))
    if need_sub:
        lines += ["", texts.INVITE_NEED_SUB.format(channel=html.escape(s.channel))]
        rows += channel_buttons(s)
    friends = await referrals_of(session, db_user.id)
    if friends:
        lines.append(f"\n<b>Приглашённые ({len(friends)}):</b>")
        for ref, user in friends[:30]:
            lines.append(f"• {short_name(user)}: {friend_status(ref, s)}")
        if len(friends) > 30:
            lines.append(f"…и ещё {len(friends) - 30}")
    rules = (texts.INVITE_RULES.format(hold=s.channel_hold_hours, limit=s.channel_ref_daily_limit)
             if s.channel_ref_on else texts.INVITE_RULES_PAID)
    lines.append(f"\n<i>{rules}</i>")
    share = f"https://t.me/share/url?url={quote(link)}&text={quote(texts.INVITE_SHARE)}"
    await message.answer("\n".join(lines), reply_markup=ik([Btn(text="📤 Поделиться ссылкой", url=share)], *rows))


# ------------------------------------------------------------------ помощь
def help_kb(user: User) -> InlineKeyboardMarkup:
    return ik([cb(texts.GUIDE_BTN, MenuCb(action="guide"))],
              [cb("⏰ Сроки и цены", MenuCb(action="h_prices")), cb("🎁 Бонусы и акции", MenuCb(action="h_bonuses"))],
              [cb("🛡 Гарантия", MenuCb(action="h_guarantee")), cb("💳 Оплата", MenuCb(action="h_payment"))],
              [cb("📄 Условия", MenuCb(action="h_terms")), cb(texts.HELP_COMMANDS_BTN, MenuCb(action="h_commands"))],
              [cb("💬 Поддержка", MenuCb(action="support")), cb("💳 Вопрос по оплате", MenuCb(action="paysupport"))],
              [cb("📱 Обновить номер" if user.phone else "📱 Оставить номер", MenuCb(action="phone"))])


@menu_router.message(F.text == M["help"])
@menu_router.message(Command("help"))
async def menu_help(message: Message, state: FSMContext, db_user: User, s: RuntimeSettings) -> None:
    await state.clear()
    await message.answer(guide.help_text(s), reply_markup=help_kb(db_user))


@router.callback_query(MenuCb.filter(F.action == "help"))
async def cb_help(call: CallbackQuery, db_user: User, s: RuntimeSettings) -> None:
    await call.answer()
    await show(call, guide.help_text(s), help_kb(db_user))


def terms_screen(user: User, s: RuntimeSettings) -> tuple[str, list[list[InlineKeyboardButton]]]:
    if user.terms_accepted_at is None:
        return s.terms_text, [[cb(texts.TERMS_ACCEPT_BTN, MenuCb(action="terms_ok"))]]
    return s.terms_text + "\n\n" + texts.TERMS_ACCEPTED.format(date=fmt_dt(user.terms_accepted_at)), []


@router.callback_query(MenuCb.filter(F.action.startswith("h_")))
async def help_screen(call: CallbackQuery, callback_data: MenuCb, db_user: User, s: RuntimeSettings,
                      config: Config) -> None:
    key = callback_data.action[2:]
    rows: list[list[InlineKeyboardButton]] = []
    if key == "prices":
        text = guide.prices_text(s)
        rows.append([cb(M["order"], MenuCb(action="order"))])
    elif key == "bonuses":
        text, extra = bonuses_screen(db_user, s)
        rows += extra
    elif key == "guarantee":
        text = guide.GUARANTEE_TEXT
    elif key == "payment":
        text = guide.PAYMENT_TEXT.format(share=s.bonus_max_share_pct)
        rows.append([cb("💳 Вопрос по оплате", MenuCb(action="paysupport"))])
    elif key == "terms":
        text, rows = terms_screen(db_user, s)
    elif key == "commands":  # тот же список, что и /commands, - только доступное этому человеку
        text = commands_text(config, call.from_user.id)
    else:
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    rows.append([cb(texts.BTN_BACK, MenuCb(action="help"))])
    await show(call, text, ik(*rows))


@menu_router.message(Command("terms"))
async def cmd_terms(message: Message, state: FSMContext, db_user: User, s: RuntimeSettings) -> None:
    await state.clear()
    text, rows = terms_screen(db_user, s)
    await message.answer(text, reply_markup=ik(*rows) if rows else None)


@router.callback_query(MenuCb.filter(F.action == "terms"))
async def cb_terms(call: CallbackQuery, db_user: User, s: RuntimeSettings) -> None:
    await call.answer()
    text, rows = terms_screen(db_user, s)
    await call.message.answer(text, reply_markup=ik(*rows) if rows else None)


@router.callback_query(MenuCb.filter(F.action == "terms_ok"))
async def cb_terms_ok(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    if db_user.terms_accepted_at is None:
        db_user.terms_accepted_at = now_utc()
        await session.commit()
    await call.answer("Принято ✅")
    try:
        await call.message.edit_reply_markup(reply_markup=drop_button(call.message.reply_markup, call.data))
    except Exception:  # noqa: BLE001
        pass
    await call.message.answer(texts.TERMS_ACCEPTED.format(date=fmt_dt(db_user.terms_accepted_at)))


# ------------------------------------------------------------------ номер телефона (только если человек сам поделился)
def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    return ("+" + digits)[:32] if digits else ""


@router.callback_query(MenuCb.filter(F.action == "phone"))
async def cb_phone(call: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await call.answer()
    text = texts.PHONE_PROMPT
    if db_user.phone:
        text += f"\n\nСейчас сохранён: <code>{html.escape(db_user.phone)}</code>"
    await call.message.answer(text, reply_markup=phone_kb())


@menu_router.message(F.contact)
async def got_contact(message: Message, session: AsyncSession, db_user: User) -> None:
    contact = message.contact
    if contact.user_id != message.from_user.id:
        await message.answer(texts.PHONE_NOT_OWN, reply_markup=phone_kb())
        return
    phone = normalize_phone(contact.phone_number)
    if not phone:
        await message.answer(texts.PHONE_NOT_OWN, reply_markup=phone_kb())
        return
    db_user.phone = phone
    await session.commit()
    await message.answer(texts.PHONE_SAVED, reply_markup=main_menu())


@menu_router.message(F.text == texts.BTN_BACK)
async def reply_back(message: Message) -> None:
    await message.answer(texts.MENU_HINT, reply_markup=main_menu())


# ------------------------------------------------------------------ /commands - что доступно именно мне
@menu_router.message(Command("commands"))
async def cmd_commands(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer(commands_text(config, message.from_user.id))


# ------------------------------------------------------------------ /admin и команды админов для не-админов
async def not_admin(message: Message, is_admin: bool) -> bool:
    return not is_admin


@menu_router.message(Command(*(c for c in ADMIN_COMMAND_NAMES if c != "admin")), not_admin)
async def cmd_staff_denied(message: Message, state: FSMContext) -> None:
    """/give, /ban, /user… от обычного пользователя - сервер отказывает, даже если команду набрали руками."""
    await state.clear()
    await message.answer(texts.ADMIN_CMD_ONLY)


@menu_router.message(Command("admin"), not_admin)
async def cmd_admin_denied(message: Message, command: CommandObject, session: AsyncSession, config: Config,
                           bot: Bot, owner_claim: owner.OwnerClaim) -> None:
    if not owner_claim.active:  # админ уже есть - чужим просто «нет доступа»
        await message.answer(texts.ADMIN_ONLY)
        return
    if not owner_claim.check(command.args):
        await message.answer(texts.OWNER_HINT)
        return
    await owner.grant(session, config, message.from_user.id)
    await refresh_commands(bot, config, message.from_user.id)
    log.warning("назначен администратор: %s (id %s)", message.from_user.full_name, message.from_user.id)
    set_menu_admin(True)
    await message.answer(texts.OWNER_GRANTED, reply_markup=main_menu())


@menu_router.message(F.text == texts.ADMIN_MENU_BTN, not_admin)
async def admin_btn_denied(message: Message) -> None:
    await message.answer(texts.ADMIN_ONLY, reply_markup=main_menu())


# ------------------------------------------------------------------ «напомнить, когда откроется»
@router.callback_query(MenuCb.filter(F.action == "remind"))
async def cb_remind(call: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    db_user.remind_tomorrow = True
    await session.commit()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(texts.REMIND_OK)


# ------------------------------------------------------------------ пользователь заблокировал / разблокировал бота
@router.my_chat_member(F.chat.type == "private", ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def bot_blocked(event: ChatMemberUpdated, session: AsyncSession, db_user: User) -> None:
    db_user.bot_blocked = True
    await session.commit()


@router.my_chat_member(F.chat.type == "private", ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def bot_unblocked(event: ChatMemberUpdated, session: AsyncSession, db_user: User) -> None:
    if db_user.bot_blocked:
        db_user.bot_blocked = False
        await session.commit()


# ------------------------------------------------------------------ всё остальное
@fallback_router.callback_query()
async def stale_callback(call: CallbackQuery) -> None:
    await call.answer(texts.STALE_BUTTON)


@fallback_router.message(StateFilter(None))
async def unknown_message(message: Message) -> None:
    text = texts.UNKNOWN_FILE if (message.photo or message.document) else texts.UNKNOWN_TEXT
    await message.answer(text, reply_markup=main_menu())
