"""Канал @aegis_samara: события подписки/отписки, кнопка «✅ Я подписался — проверить», бонусы за друзей.

Бот должен быть администратором канала — иначе Telegram не присылает события и не даёт проверить подписку.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import CallbackQuery, Chat, ChatMemberUpdated, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import MenuCb
from app.bot.keyboards import Btn, cb, ik
from app.bot.notify import notify_admins, safe_send
from app.config import Config
from app.db.models import User
from app.services import channel as ch
from app.services.locks import app_lock
from app.services.settings import RuntimeSettings
from app.services.users import short_name
from app.utils.money import fmt_rub
from app.utils.timeutil import fmt_date, now_utc

log = logging.getLogger(__name__)
router = Router(name="channel")


def is_our_channel(chat: Chat, s: RuntimeSettings) -> bool:
    channel = s.channel
    if not channel or chat.type not in ("channel", "supergroup"):
        return False
    if channel.startswith("@"):
        return (chat.username or "").lower() == channel[1:].lower()
    return str(chat.id) == channel


# ------------------------------------------------------------------ блок «бонус за подписку» для экранов
def channel_buttons(s: RuntimeSettings, check: bool = True) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if s.channel_url:
        rows.append([Btn(text=texts.CHANNEL_OPEN_BTN, url=s.channel_url)])
    if check and s.channel:
        rows.append([cb(texts.CHANNEL_CHECK_BTN, MenuCb(action="ch_check"))])
    return rows


def channel_promo(user: User, s: RuntimeSettings) -> tuple[str, list[list[InlineKeyboardButton]]]:
    if not s.channel:
        return "", []
    channel = html.escape(s.channel)
    if s.channel_bonus_on and user.channel_bonus_at is None and user.channel_bonus_revoked_at is None:
        return texts.CHANNEL_PROMO.format(channel=channel, bonus=fmt_rub(int(s.channel_bonus))), channel_buttons(s)
    if user.channel_bonus_at is not None and user.channel_bonus_revoked_at is None:
        return texts.CHANNEL_PROMO_DONE.format(channel=channel), channel_buttons(s, check=False)
    return f"📢 Наш канал {channel} — разборы, лайфхаки к сессии и акции.", channel_buttons(s, check=False)


# ------------------------------------------------------------------ уведомления
async def send_ref_events(bot: Bot, session: AsyncSession, s: RuntimeSettings, events: list[ch.RefEvent]) -> None:
    for e in events:
        friend = await session.get(User, e.friend_id)
        name = short_name(friend)
        if e.kind == "reward":
            await safe_send(bot, e.referrer_id, texts.REF_SUB_REWARD.format(
                amount=fmt_rub(e.amount), name=name, date=fmt_date(e.expires_at)))
        elif e.kind == "need_sub":
            await safe_send(bot, e.referrer_id, texts.REF_SUB_NEED_YOU.format(
                amount=fmt_rub(e.amount), name=name, channel=html.escape(s.channel)),
                reply_markup=ik(*channel_buttons(s)))


async def notify_referrer(bot: Bot, session: AsyncSession, s: RuntimeSettings, res: ch.JoinResult,
                          friend: User) -> None:
    if res.referrer_id is None:
        return
    referrer = await session.get(User, res.referrer_id)
    if referrer is None:
        return
    status = referrer.channel_status
    if status != ch.MEMBER:
        status = await ch.fetch_status(bot, s.channel, referrer.id, now_utc()) or status
        if status:
            referrer.channel_status, referrer.channel_checked_at = status, now_utc()
            await session.commit()
    name = short_name(friend)
    if status == ch.MEMBER:
        text = texts.REF_FRIEND_SUBSCRIBED.format(name=name, amount=fmt_rub(res.ref_amount),
                                                  date=fmt_date(res.ref_due_at))
        await safe_send(bot, referrer.id, text)
    else:
        text = texts.REF_FRIEND_SUBSCRIBED_NEED_SUB.format(name=name, amount=fmt_rub(res.ref_amount),
                                                           channel=html.escape(s.channel))
        await safe_send(bot, referrer.id, text, reply_markup=ik(*channel_buttons(s)))


async def alert_check_failed(bot: Bot, config: Config, s: RuntimeSettings) -> None:
    if ch.should_alert(now_utc()):
        await notify_admins(bot, config, (
            f"⚠️ Не получается проверить подписку на {html.escape(s.channel)}: {html.escape(ch.health.error or '')}\n"
            "Сделайте бота администратором канала (достаточно минимальных прав) — иначе бонусы за подписку "
            "не начисляются. Канал меняется в «⚙️ Настройки» → «📢 Канал и бонусы за подписку»."), min_level=2)


async def joined(bot: Bot, session: AsyncSession, s: RuntimeSettings, user_id: int, *,
                 checker_fresh: bool = True) -> ch.JoinResult | None:
    """Подписка подтверждена: бонус себе, отметка у друга-пригласившего, выдача ждущих бонусов за друзей."""
    if await session.get(User, user_id) is None:
        return None  # подписался на канал, но ботом ещё не пользовался — бонус получит по кнопке «проверить»
    now = now_utc()
    async with app_lock():
        res = await ch.on_member(session, user_id, s, now)

        async def checker(uid: int) -> str | None:
            return await ch.fetch_status(bot, s.channel, uid, now)

        report = await ch.process_due(session, s, now, checker, referrer_id=user_id, fresh=checker_fresh)
    user = await session.get(User, user_id, populate_existing=True)
    if res.granted:
        await safe_send(bot, user_id, texts.CHANNEL_GRANTED.format(
            channel=html.escape(s.channel), amount=fmt_rub(res.granted), date=fmt_date(res.expires_at),
            hold=s.channel_hold_hours))
    await notify_referrer(bot, session, s, res, user)
    await send_ref_events(bot, session, s, report.events)
    return res


# ------------------------------------------------------------------ события канала
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def channel_join(event: ChatMemberUpdated, session: AsyncSession, s: RuntimeSettings, bot: Bot) -> None:
    if not is_our_channel(event.chat, s) or event.new_chat_member.user.is_bot:
        return
    await joined(bot, session, s, event.new_chat_member.user.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def channel_leave(event: ChatMemberUpdated, session: AsyncSession, s: RuntimeSettings, bot: Bot) -> None:
    if not is_our_channel(event.chat, s) or event.new_chat_member.user.is_bot:
        return
    uid = event.new_chat_member.user.id
    if await session.get(User, uid) is None:
        return
    async with app_lock():
        res = await ch.on_left(session, uid, s, now_utc())
    if res.was_granted:
        await safe_send(bot, uid, texts.CHANNEL_LEFT_REVOKED.format(
            channel=html.escape(s.channel), hold=s.channel_hold_hours, amount=fmt_rub(res.revoked)))


@router.my_chat_member(F.chat.type.in_({"channel", "supergroup"}))
async def bot_rights_changed(event: ChatMemberUpdated, s: RuntimeSettings, bot: Bot, config: Config) -> None:
    if not is_our_channel(event.chat, s):
        return
    status = str(getattr(event.new_chat_member.status, "value", event.new_chat_member.status))
    if status == "administrator":
        ch.health.error = None
        text = (f"✅ Бот теперь администратор канала {html.escape(s.channel)} — подписки проверяются, "
                "бонусы начисляются автоматически.")
    else:
        text = (f"⚠️ Бот больше не администратор канала {html.escape(s.channel)} — проверка подписки и бонусы "
                "за неё не работают. Верните боту права администратора.")
    await notify_admins(bot, config, text, min_level=2)


# ------------------------------------------------------------------ «✅ Я подписался — проверить»
@router.callback_query(MenuCb.filter(F.action == "ch_check"))
async def cb_check(call: CallbackQuery, session: AsyncSession, s: RuntimeSettings, bot: Bot, config: Config,
                   db_user: User) -> None:
    if not s.channel:
        await call.answer(texts.CHANNEL_DISABLED, show_alert=True)
        return
    status = await ch.fetch_status(bot, s.channel, db_user.id, now_utc())
    if status is None:
        await call.answer(texts.CHANNEL_CHECK_FAILED, show_alert=True)
        await alert_check_failed(bot, config, s)
        return
    if status == ch.LEFT:
        db_user.channel_status, db_user.channel_checked_at = ch.LEFT, now_utc()
        await session.commit()
        await call.answer(texts.CHANNEL_NOT_MEMBER.format(channel=s.channel), show_alert=True)
        return
    res = await joined(bot, session, s, db_user.id)
    if res is None:
        await call.answer(texts.STALE_BUTTON)
        return
    if res.granted:
        await call.answer("🎉 Бонус начислен!")
    elif res.revoked_before:
        await call.answer(texts.CHANNEL_REVOKED_BEFORE, show_alert=True)
    elif res.already_at is not None:
        await call.answer(texts.CHANNEL_ALREADY.format(date=fmt_date(res.already_at)), show_alert=True)
    else:
        await call.answer("✅ Подписка есть — спасибо, что с нами!", show_alert=True)
    if call.message is not None and call.message.reply_markup is not None:
        rows = [row for row in call.message.reply_markup.inline_keyboard
                if not any(b.callback_data == MenuCb(action="ch_check").pack() for b in row)]
        try:
            await call.message.edit_reply_markup(reply_markup=ik(*rows) if rows else None)
        except Exception:  # noqa: BLE001
            pass
