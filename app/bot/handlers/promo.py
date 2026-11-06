"""Задания v2.2 у пользователя: «📌 Ссылка в профиле», «📣 Пост о нас», «📣 Мои посты».

Вход - «❓ Помощь» / «💰 Баланс» → «🎁 Бонусы и акции». Ожидание скрина или ссылки - состояние PromoSG;
любая кнопка меню, команда или кнопка другого раздела тихо его отменяет (см. UserMiddleware) - без потерь:
вернуться к посту можно через «📣 Мои посты» в любой момент.
"""
from __future__ import annotations

import html
import re
import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import MenuCb, PromoCb
from app.bot.handlers.admin.tasks import notify_claim, notify_report
from app.bot.keyboards import cb, ik
from app.bot.states import PromoSG
from app.bot.ui import show
from app.config import Config
from app.db.models import PromoPost, User
from app.services import promo
from app.services.settings import RuntimeSettings
from app.utils.money import fmt_rub
from app.utils.timeutil import now_utc

router = Router(name="promo")
_albums: dict[tuple[int, str], float] = {}  # (пользователь, альбом) → когда приняли первый скрин из альбома
ALBUM_TTL = 120.0


def P(action: str, pid: int = 0, v: str = "") -> PromoCb:  # noqa: N802
    return PromoCb(action=action, id=pid, v=v)


def back_btn():  # noqa: ANN201
    return cb(texts.BTN_BACK, MenuCb(action="h_bonuses"))


def cancel_kb() -> InlineKeyboardMarkup:
    return ik([cb(texts.BTN_CANCEL, P("cancel"))])


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))[:200]


def reason_line(reason: str | None) -> str:
    return texts.REJECT_REASON_LINE.format(reason=html.escape(reason)) if reason else ""


def image_of(message: Message) -> tuple[str, str] | None:
    """Скрин: фото или картинка файлом → (file_id, photo|document)."""
    if message.photo:
        return message.photo[-1].file_id, "photo"
    doc = message.document
    if doc is not None and (doc.mime_type or "").startswith("image/"):
        return doc.file_id, "document"
    return None


def _album_seen(message: Message) -> bool:
    """Альбом из нескольких скринов: берём первый, остальные молча пропускаем. Вызывать до первого await -     апдейты альбома обрабатываются параллельно."""
    if not message.media_group_id or message.from_user is None:
        return False
    now = time.monotonic()
    for key in [k for k, t in _albums.items() if now - t > ALBUM_TTL]:
        _albums.pop(key, None)
    key = (message.from_user.id, message.media_group_id)
    if key in _albums:
        return True
    _albums[key] = now
    return False


async def extra_album_photo(message: Message) -> bool:
    if not message.media_group_id or message.from_user is None:
        return False
    return (message.from_user.id, message.media_group_id) in _albums


# ------------------------------------------------------------------ «📌 Ссылка в профиле»
async def profile_screen(session: AsyncSession, user: User, s: RuntimeSettings,
                         bot_username: str) -> tuple[str, InlineKeyboardMarkup]:
    reward = fmt_rub(int(s.profile_task_reward))
    block = await promo.profile_block(session, user.id, s)
    done = await promo.profile_done(session, user.id)
    title = texts.PROFILE_TITLE.format(reward=fmt_rub(done.reward) if done is not None else reward)
    if block == texts.PROFILE_DISABLED:
        return texts.PROFILE_DISABLED, ik([back_btn()])
    if done is not None:
        return f"{title}\n\n{block}\n\n{s.withdraw_note}", ik([back_btn()])
    link = promo.ref_link(bot_username, user)
    lines = [title, "", texts.PROFILE_BODY.format(link=html.escape(link), reward=reward)]
    if block:
        lines += ["", block]
    else:
        last = await promo.latest_claim(session, user.id)
        if last is not None and last.status == "rejected":
            lines += ["", texts.PROFILE_LAST_REJECTED.format(reason=reason_line(last.reject_reason))]
    lines += ["", s.withdraw_note]
    rows = [] if block else [[cb(texts.PROFILE_SEND_BTN, P("profile_send"))]]
    rows.append([back_btn()])
    return "\n".join(lines), ik(*rows)


@router.callback_query(PromoCb.filter(F.action == "profile"))
async def pr_profile(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                     s: RuntimeSettings, bot_username: str) -> None:
    await state.clear()
    await call.answer()
    await show(call, *await profile_screen(session, db_user, s, bot_username))


@router.callback_query(PromoCb.filter(F.action == "profile_send"))
async def pr_profile_send(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                          s: RuntimeSettings, bot_username: str) -> None:
    block = await promo.profile_block(session, db_user.id, s)
    if block:
        await state.clear()
        await call.answer(plain(block), show_alert=True)
        await show(call, *await profile_screen(session, db_user, s, bot_username))
        return
    await call.answer()
    await state.set_state(PromoSG.profile_shot)
    await call.message.answer(texts.PROFILE_SHOT_PROMPT, reply_markup=cancel_kb())


@router.message(PromoSG.profile_shot)
async def pr_profile_shot(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                          s: RuntimeSettings, bot: Bot, config: Config, bot_username: str) -> None:
    shot = image_of(message)
    if shot is None:
        await message.answer(texts.PROMO_WAIT_FILE, reply_markup=cancel_kb())
        return
    if _album_seen(message):
        return
    await state.clear()
    bio = await promo.check_bio(bot, db_user.id, db_user.ref_code, bot_username)
    try:
        claim = await promo.submit_profile(session, db_user, shot[0], shot[1], promo.ref_link(bot_username, db_user),
                                           bio, s, now_utc())
    except promo.PromoError as e:
        await message.answer(str(e))
        return
    await notify_claim(bot, session, config, claim, db_user, s)
    await session.commit()
    await message.answer(texts.PROFILE_SENT.format(reward=fmt_rub(int(s.profile_task_reward))))


# ------------------------------------------------------------------ «📣 Пост о нас»
def cap_note(s: RuntimeSettings) -> str:
    cap = int(s.post_views_cap)
    return texts.POST_CAP_NOTE.format(cap=fmt_rub(cap)) if cap > 0 else ""


async def post_screen(session: AsyncSession, user: User, s: RuntimeSettings,
                      bot_username: str) -> tuple[str, InlineKeyboardMarkup]:
    block = await promo.post_block(session, user.id, s)
    if block == texts.POST_DISABLED:
        return texts.POST_DISABLED, ik([cb(texts.MY_POSTS_BTN, P("posts"))], [back_btn()])
    base, per, step = fmt_rub(int(s.post_base_reward)), fmt_rub(int(s.post_views_reward)), int(s.post_views_step)
    template = promo.render_template(s.post_template, promo.ref_link(bot_username, user))
    lines = [texts.POST_TITLE.format(base=base, per=per, step=step), "",
             texts.POST_BODY.format(base=base, per=per, step=step, cap=cap_note(s), active=s.post_max_active), "",
             texts.POST_TEMPLATE_HEAD, f"<pre>{html.escape(template)}</pre>", "", s.withdraw_note, ""]
    rows = []
    if block:
        lines.append(block)
    else:
        lines.append(texts.POST_CHOOSE)
        rows.append([cb(texts.POST_TG_BTN, P("plat", v="tg")), cb(texts.POST_VK_BTN, P("plat", v="vk"))])
    rows += [[cb(texts.MY_POSTS_BTN, P("posts"))], [back_btn()]]
    return "\n".join(lines), ik(*rows)


@router.callback_query(PromoCb.filter(F.action == "post"))
async def pr_post(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User,
                  s: RuntimeSettings, bot_username: str) -> None:
    await state.clear()
    await call.answer()
    await show(call, *await post_screen(session, db_user, s, bot_username))


@router.callback_query(PromoCb.filter(F.action == "plat"))
async def pr_platform(call: CallbackQuery, callback_data: PromoCb, state: FSMContext, session: AsyncSession,
                      db_user: User, s: RuntimeSettings) -> None:
    platform = callback_data.v if callback_data.v in texts.POST_PLATFORMS else "tg"
    block = await promo.post_block(session, db_user.id, s)
    if block:
        await state.clear()
        await call.answer(plain(block), show_alert=True)
        return
    await call.answer()
    await state.set_state(PromoSG.post_url)
    await state.update_data(platform=platform)
    await call.message.answer(texts.POST_URL_PROMPT[platform], reply_markup=cancel_kb())


def _url_candidates(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    out = []
    for e in (message.entities or message.caption_entities or []):
        if e.type == "text_link" and e.url:
            out.append(e.url)
        elif e.type == "url":
            out.append(e.extract_from(text))
    return out + [text]


@router.message(PromoSG.post_url)
async def pr_post_url(message: Message, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    platform = (await state.get_data()).get("platform", "tg")
    found = next((r for r in map(promo.normalize_post_url, _url_candidates(message)) if r), None)
    if found is None:
        await message.answer(texts.POST_URL_BAD.get(platform, texts.POST_URL_BAD["tg"]), reply_markup=cancel_kb())
        return
    platform, url = found  # платформу берём по самой ссылке: прислали ВК вместо TG - тоже ок
    try:
        await promo.check_url(session, db_user.id, url)
    except promo.PromoError as e:
        await message.answer(str(e), reply_markup=cancel_kb())
        return
    await state.set_state(PromoSG.post_shot)
    await state.update_data(platform=platform, url=url)
    await message.answer(texts.POST_SHOT_PROMPT, reply_markup=cancel_kb())


@router.message(PromoSG.post_shot)
async def pr_post_shot(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                       s: RuntimeSettings, bot: Bot, config: Config) -> None:
    shot = image_of(message)
    if shot is None:
        await message.answer(texts.PROMO_WAIT_FILE, reply_markup=cancel_kb())
        return
    if _album_seen(message):
        return
    data = await state.get_data()
    await state.clear()
    platform, url = data.get("platform", "tg"), data.get("url")
    if not url:
        await message.answer(texts.STALE_BUTTON)
        return
    try:
        post, report = await promo.submit_post(session, db_user, platform, url, shot[0], shot[1], s, now_utc())
    except promo.PromoError as e:
        await message.answer(str(e), reply_markup=ik([cb(texts.MY_POSTS_BTN, P("posts"))]))
        return
    await notify_report(bot, session, config, report, post, db_user, s)
    await session.commit()
    await message.answer(texts.POST_SENT.format(base=fmt_rub(int(s.post_base_reward))),
                         reply_markup=ik([cb(texts.MY_POSTS_BTN, P("posts"))]))


# ------------------------------------------------------------------ «📣 Мои посты» и отчёты о просмотрах
async def posts_screen(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup]:
    posts = await promo.user_posts(session, user.id)
    lines = [texts.MY_POSTS_TITLE, ""]
    rows = []
    if not posts:
        lines.append(texts.MY_POSTS_EMPTY)
    for p in posts:
        status = texts.POST_STATUS.get(p.status, p.status)
        if p.status == "rejected" and p.reject_reason:
            status += f": {html.escape(p.reject_reason)}"
        lines.append(texts.MY_POSTS_LINE.format(
            icon="🟦" if p.platform == "vk" else "📣", id=p.id, platform=texts.POST_PLATFORMS.get(p.platform, p.platform),
            url=html.escape(p.post_url), status=status, views=p.verified_views, credited=fmt_rub(p.credited)))
        if p.status == "accepted":
            rows.append([cb(f"{texts.VIEWS_BTN} · №{p.id}", P("views", p.id))])
    if rows:
        lines += ["", texts.MY_POSTS_HINT]
    rows += [[cb(texts.POST_BTN, P("post"))], [back_btn()]]
    return "\n".join(lines), ik(*rows)


@router.callback_query(PromoCb.filter(F.action == "posts"))
async def pr_posts(call: CallbackQuery, state: FSMContext, session: AsyncSession, db_user: User) -> None:
    await state.clear()
    await call.answer()
    await show(call, *await posts_screen(session, db_user))


@router.callback_query(PromoCb.filter(F.action == "views"))
async def pr_views(call: CallbackQuery, callback_data: PromoCb, state: FSMContext, session: AsyncSession,
                   db_user: User, s: RuntimeSettings) -> None:
    await state.clear()
    post = await session.get(PromoPost, callback_data.id, populate_existing=True)
    if post is None or post.user_id != db_user.id:
        await call.answer(texts.NOT_FOUND, show_alert=True)
        return
    block = await promo.views_block(session, post, s, now_utc())
    if block:
        await call.answer(plain(block), show_alert=True)
        return
    await call.answer()
    await state.set_state(PromoSG.views_shot)
    await state.update_data(post_id=post.id)
    await call.message.answer(texts.VIEWS_SHOT_PROMPT.format(views=post.verified_views), reply_markup=cancel_kb())


@router.message(PromoSG.views_shot)
async def pr_views_shot(message: Message, state: FSMContext) -> None:
    shot = image_of(message)
    if shot is None:
        await message.answer(texts.PROMO_WAIT_FILE, reply_markup=cancel_kb())
        return
    if _album_seen(message):
        return
    await state.set_state(PromoSG.views_count)
    await state.update_data(file_id=shot[0], file_type=shot[1])
    await message.answer(texts.VIEWS_COUNT_PROMPT, reply_markup=cancel_kb())


@router.message(PromoSG.views_count, extra_album_photo)
async def pr_views_album_rest(message: Message) -> None:
    """Остальные фото из альбома со скрином - не ответ на «сколько просмотров»."""


@router.message(PromoSG.views_count)
async def pr_views_count(message: Message, state: FSMContext, session: AsyncSession, db_user: User,
                         s: RuntimeSettings, bot: Bot, config: Config) -> None:
    raw = re.sub(r"[\s\u00a0]", "", message.text or "")
    data = await state.get_data()
    post = await session.get(PromoPost, int(data.get("post_id") or 0), populate_existing=True)
    if post is None or post.user_id != db_user.id:
        await state.clear()
        await message.answer(texts.NOT_FOUND)
        return
    if not raw.isdigit() or int(raw) > promo.MAX_VIEWS:
        await message.answer(texts.VIEWS_COUNT_BAD, reply_markup=cancel_kb())
        return
    claimed = int(raw)
    if claimed <= post.verified_views:
        await message.answer(texts.VIEWS_NOT_MORE.format(n=post.verified_views), reply_markup=cancel_kb())
        return
    await state.clear()
    try:
        post, report = await promo.submit_views(session, db_user.id, post.id, data["file_id"], data["file_type"],
                                                claimed, s, now_utc())
    except promo.PromoError as e:
        await message.answer(str(e), reply_markup=ik([cb(texts.MY_POSTS_BTN, P("posts"))]))
        return
    await notify_report(bot, session, config, report, post, db_user, s)
    await session.commit()
    await message.answer(texts.VIEWS_SENT.format(n=claimed), reply_markup=ik([cb(texts.MY_POSTS_BTN, P("posts"))]))


# ------------------------------------------------------------------ отмена и всё остальное
@router.callback_query(PromoCb.filter(F.action == "cancel"))
async def pr_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await show(call, texts.PROMO_CANCELLED, ik([cb(texts.BALANCE_BONUSES_BTN, MenuCb(action="h_bonuses"))]))


@router.message(F.media_group_id, extra_album_photo)
async def album_rest(message: Message) -> None:
    """Остальные фото из альбома, первый из которого уже приняли как скрин, - без «не понял»."""
