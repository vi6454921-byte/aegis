"""Админка → 🎯 Задания (1 и 2 уровень): «📌 Ссылка в профиле» и «📣 Пост о нас».

Как у чеков: карточка со скрином приходит всем админам, решение одного помечает карточки у остальных
(«✅ Начислено: Аня»), повторное нажатие ничего не начисляет. Деньги — обычные рубли через ledger (services.promo).
Ручная корректировка по посту — только 2 уровень.
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.bot.callbacks import AdminCb, PromoCb
from app.bot.handlers.admin.base import L2, admin_router, ask, cancel_btn, close_prompt, menu_btn
from app.bot.keyboards import cb, ik
from app.bot.notify import mark_headers, remember, safe_send, staff_targets
from app.bot.states import AdminSG
from app.bot.ui import show
from app.config import Config
from app.db.models import ProfileClaim, PromoPost, PromoPostReport, User
from app.services import admin_money
from app.services import promo
from app.services.settings import RuntimeSettings
from app.services.users import display_name, short_name
from app.utils.money import fmt_rub, split_amount
from app.utils.timeutil import fmt_dt, now_utc, to_local

log = logging.getLogger(__name__)
router = admin_router("admin_tasks")
KIND_PROFILE, KIND_PROMO = "profile", "promo"  # AdminMessage.kind: заявка профиля / отчёт по посту
REASONS_FOR = {"pc": ("blur", "nolink", "foreign", "fake"), "first": ("blur", "nolink", "foreign", "fake"),
               "views": ("blur", "views", "foreign", "fake")}


def T(a: str = "", rid: int = 0, v: str = "") -> AdminCb:  # noqa: N802
    return AdminCb(s="pt", a=a, id=rid, v=v)


def _f(action: str):  # noqa: ANN202
    return AdminCb.filter((F.s == "pt") & (F.a == action))


def reason_line(reason: str | None) -> str:
    return texts.REJECT_REASON_LINE.format(reason=html.escape(reason)) if reason else ""


def plain(text: str) -> str:
    """Текст для всплывающего окна: без HTML-тегов."""
    import re  # noqa: PLC0415

    return html.unescape(re.sub(r"<[^>]+>", "", text))[:200]


async def _stamp(session: AsyncSession, admin_id: int) -> str:
    admin = await session.get(User, admin_id)
    return f"{short_name(admin)} · {to_local(now_utc()):%H:%M}"


def _client_btn(user_id: int):  # noqa: ANN202
    return cb("👤 Клиент", AdminCb(s="u", a="open", id=user_id))


# ------------------------------------------------------------------ карточки
def claim_header(claim: ProfileClaim, user: User | None, s: RuntimeSettings) -> str:
    reward = claim.reward if claim.status in ("approved", "revoked") else int(s.profile_task_reward)
    text = texts.ADMIN_PROFILE_CLAIM.format(
        id=claim.id, user=display_name(user), reward=fmt_rub(reward), link=html.escape(claim.link or "—"),
        bio=texts.BIO_CHECK.get(claim.bio_check or "unknown", texts.BIO_CHECK["unknown"]))
    text += f"\nСкрин: {'прислан ⬆️' if claim.file_id else 'нет'} · {fmt_dt(claim.created_at)}"
    return text + "\n<i>Проверь на скрине ссылку на бота в «О себе». Автопроверка — подсказка, решаешь ты.</i>"


def report_header(report: PromoPostReport, post: PromoPost, user: User | None, s: RuntimeSettings) -> str:
    platform = texts.POST_PLATFORMS.get(post.platform, post.platform)
    url = html.escape(post.post_url)
    if report.kind == "first":
        text = texts.ADMIN_POST_FIRST.format(id=post.id, platform=platform, user=display_name(user), url=url,
                                             base=fmt_rub(int(s.post_base_reward)))
        hint = "<i>Проверь: пост опубликован, в нём есть ссылка на бота, ссылка ведёт на этот пост.</i>"
    else:
        claimed = int(report.claimed_views or 0)
        pay = max(promo.views_total(claimed, s) - int(post.views_reward_paid or 0), 0)
        text = texts.ADMIN_POST_VIEWS.format(
            id=post.id, platform=platform, user=display_name(user), url=url, claimed=claimed,
            prev=post.verified_views, credited=fmt_rub(post.credited), views_paid=fmt_rub(post.views_reward_paid),
            pay=fmt_rub(pay))
        hint = "<i>Сверь счётчик просмотров на скрине. Подтвердить можно своё число — не меньше прошлого.</i>"
    text += f"\nСкрин: {'прислан ⬆️' if report.file_id else 'нет'} · {fmt_dt(report.created_at)}"
    return f"{text}\n{hint}"


def claim_kb(claim: ProfileClaim) -> InlineKeyboardMarkup:
    return ik([cb(texts.TASK_PAY_BTN, T("pc_ok", claim.id)), cb(texts.TASK_REJECT_BTN, T("no", claim.id, "pc"))],
              [_client_btn(claim.user_id)])


def report_kb(report: PromoPostReport, post: PromoPost) -> InlineKeyboardMarkup:
    if report.kind == "first":
        first = [cb(texts.TASK_ACCEPT_BTN, T("rp_ok", report.id)), cb(texts.TASK_REJECT_BTN, T("no", report.id, "rp"))]
        return ik(first, [cb(texts.TASK_POST_BTN.format(id=post.id), T("post", post.id)), _client_btn(post.user_id)])
    return ik([cb(texts.TASK_VIEWS_OK_BTN.format(n=report.claimed_views or 0), T("rv_ok", report.id))],
              [cb(texts.TASK_VIEWS_NUM_BTN, T("rv_num", report.id)), cb(texts.TASK_REJECT_BTN, T("no", report.id, "rp"))],
              [cb(texts.TASK_POST_BTN.format(id=post.id), T("post", post.id)), _client_btn(post.user_id)])


def done_kb(user_id: int, post_id: int | None = None):  # noqa: ANN201
    """Кнопки под карточкой после решения (MarkupFor: уровень → кнопки)."""
    def make(level: int) -> InlineKeyboardMarkup:
        row = [_client_btn(user_id)]
        if post_id:
            row.insert(0, cb(texts.TASK_POST_BTN.format(id=post_id), T("post", post_id)))
        return ik(row)
    return make


async def _send_shot(bot: Bot, chat_id: int, file_id: str | None, file_type: str | None, caption: str,
                     kb: InlineKeyboardMarkup | None) -> Message | None:
    try:
        if file_id and file_type == "photo":
            return await bot.send_photo(chat_id, file_id, caption=caption[:1024], reply_markup=kb)
        if file_id and file_type == "document":
            return await bot.send_document(chat_id, file_id, caption=caption[:1024], reply_markup=kb)
    except Exception:  # noqa: BLE001 — скрин не ушёл: карточка всё равно нужна
        log.warning("скрин задания не отправился админу %s", chat_id)
    return await safe_send(bot, chat_id, caption, reply_markup=kb)


async def notify_claim(bot: Bot, session: AsyncSession, config: Config, claim: ProfileClaim, user: User,
                       s: RuntimeSettings) -> None:
    """Заявка «Ссылка в профиле» — всем админам 1 и 2 уровня: скрин с подписью и кнопками. Без commit."""
    header = claim_header(claim, user, s)
    for admin_id, _lvl in staff_targets(config, 1):
        msg = await _send_shot(bot, admin_id, claim.file_id, claim.file_type, header, claim_kb(claim))
        if msg is not None:
            remember(session, admin_id, msg.message_id, KIND_PROFILE, claim.id, user.id, header)


async def notify_report(bot: Bot, session: AsyncSession, config: Config, report: PromoPostReport, post: PromoPost,
                        user: User, s: RuntimeSettings) -> None:
    """Пост или отчёт о просмотрах — всем админам 1 и 2 уровня. Без commit."""
    header = report_header(report, post, user, s)
    for admin_id, _lvl in staff_targets(config, 1):
        msg = await _send_shot(bot, admin_id, report.file_id, report.file_type, header, report_kb(report, post))
        if msg is not None:
            remember(session, admin_id, msg.message_id, KIND_PROMO, report.id, user.id, header)


# ------------------------------------------------------------------ экраны
async def queue_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    counts = await promo.pending_counts(session)
    claims, reports = await promo.queue(session)
    lines = [texts.TASKS_TITLE, texts.TASKS_COUNTS.format(**counts)]
    rows = []
    for c in claims:
        u = await session.get(User, c.user_id)
        rows.append([cb(f"📌 №{c.id} · {short_name(u)} · {fmt_dt(c.created_at)}", T("pc", c.id))])
    for r in reports:
        post = await session.get(PromoPost, r.post_id)
        u = await session.get(User, post.user_id) if post else None
        what = "📣 пост" if r.kind == "first" else f"📈 {r.claimed_views or 0} просм."
        rows.append([cb(f"{what} №{r.post_id} · {short_name(u)} · {fmt_dt(r.created_at)}", T("rp", r.id))])
    if not claims and not reports:
        lines.append("\n" + texts.TASKS_EMPTY)
    rows.append([cb(texts.TASKS_ALL_POSTS_BTN, T("posts")), cb("🔄 Обновить", T("list"))])
    rows.append([menu_btn()])
    return "\n".join(lines), ik(*rows)


async def claim_view(session: AsyncSession, claim: ProfileClaim, s: RuntimeSettings,
                     note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    user = await session.get(User, claim.user_id)
    text = claim_header(claim, user, s)
    back = [cb("⬅️ К заданиям", T()), menu_btn()]
    if claim.status != "pending":
        text += f"\n\nСтатус: {texts.PROFILE_STATUS.get(claim.status, claim.status)} · {fmt_dt(claim.reviewed_at)}"
        if claim.reject_reason:
            text += f"\nПричина: {html.escape(claim.reject_reason)}"
        kb = ik([_client_btn(claim.user_id)], back)
    else:
        kb = claim_kb(claim)
        if claim.file_id:
            kb.inline_keyboard.insert(0, [cb(texts.TASK_SHOT_BTN, T("file", claim.id, "pc"))])
        kb.inline_keyboard.append(back)
    return (f"{note}\n\n" if note else "") + text, kb


async def report_view(session: AsyncSession, report: PromoPostReport, s: RuntimeSettings,
                      note: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
    post = await session.get(PromoPost, report.post_id, populate_existing=True)
    if post is None:
        return None
    user = await session.get(User, post.user_id)
    text = report_header(report, post, user, s)
    back = [cb("⬅️ К заданиям", T()), menu_btn()]
    if report.status != "pending":
        text += f"\n\nСтатус: {texts.REPORT_STATUS.get(report.status, report.status)} · {fmt_dt(report.reviewed_at)}"
        if report.verified_views is not None and report.kind == "views":
            text += f"\nПодтверждено: {report.verified_views} · начислено {fmt_rub(report.credited)}"
        if report.reject_reason:
            text += f"\nПричина: {html.escape(report.reject_reason)}"
        kb = ik([cb(texts.TASK_POST_BTN.format(id=post.id), T("post", post.id)), _client_btn(post.user_id)], back)
    else:
        kb = report_kb(report, post)
        if report.file_id:
            kb.inline_keyboard.insert(0, [cb(texts.TASK_SHOT_BTN, T("file", report.id, "rp"))])
        kb.inline_keyboard.append(back)
    return (f"{note}\n\n" if note else "") + text, kb


async def post_view(session: AsyncSession, post_id: int, level: int,
                    note: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
    post = await session.get(PromoPost, post_id, populate_existing=True)
    if post is None:
        return None
    user = await session.get(User, post.user_id)
    lines = [f"📣 <b>Пост №{post.id}</b> · {texts.POST_PLATFORMS.get(post.platform, post.platform)} · "
             f"{texts.POST_STATUS.get(post.status, post.status)}",
             f"От: {display_name(user)}", f"Ссылка: {html.escape(post.post_url)}",
             f"Подтверждено просмотров: <b>{post.verified_views}</b>",
             f"Начислено всего: <b>{fmt_rub(post.credited)}</b> (за пост {fmt_rub(post.base_reward_paid)}, "
             f"за просмотры {fmt_rub(post.views_reward_paid)}, корректировки {fmt_rub(post.adjust_total)})"]
    if post.reject_reason:
        lines.append(f"Причина отказа: {html.escape(post.reject_reason)}")
    if post.deleted_at:
        lines.append(f"🗑 Отмечен удалённым {fmt_dt(post.deleted_at)}")
    reports = await promo.post_reports(session, post.id)
    rows = []
    if reports:
        lines.append("\n<b>Отчёты</b>")
    for r in reports[-10:]:
        what = "первый скрин" if r.kind == "first" else f"просмотры: заявлено {r.claimed_views or 0}"
        if r.verified_views is not None and r.kind == "views":
            what += f", подтверждено {r.verified_views}"
        lines.append(f"• №{r.id} · {fmt_dt(r.created_at)} · {what} · {texts.REPORT_STATUS.get(r.status, r.status)}"
                     + (f" · +{fmt_rub(r.credited)}" if r.credited else ""))
        if r.status == "pending":
            rows.append([cb(f"⏳ Отчёт №{r.id} — проверить", T("rp", r.id))])
    actions = []
    if post.status in promo.POST_ACTIVE:
        actions.append(cb(texts.TASK_DELETED_BTN, T("del", post.id)))
    if level >= 2:
        actions.append(cb(texts.TASK_ADJUST_BTN, T("adj", post.id)))
    rows += [actions, [_client_btn(post.user_id), cb(texts.TASKS_ALL_POSTS_BTN, T("posts"))],
             [cb("⬅️ К заданиям", T()), menu_btn()]]
    return (f"{note}\n\n" if note else "") + "\n".join(lines), ik(*rows)


async def posts_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    posts = list((await session.execute(select(PromoPost).order_by(PromoPost.last_update_at.desc(),
                                                                   PromoPost.id.desc()).limit(20))).scalars())
    lines = [texts.TASKS_POSTS_TITLE.format(n=len(posts))]
    if not posts:
        lines.append("Постов пока нет.")
    rows = []
    for p in posts:
        u = await session.get(User, p.user_id)
        status = texts.POST_STATUS.get(p.status, p.status).split(" ")[0]
        rows.append([cb(f"{status} №{p.id} · {short_name(u)} · 👁 {p.verified_views} · {fmt_rub(p.credited)}",
                        T("post", p.id))])
    rows.append([cb("⬅️ К заданиям", T()), menu_btn()])
    return "\n".join(lines), ik(*rows)


# ------------------------------------------------------------------ просмотр
@router.callback_query(AdminCb.filter((F.s == "pt") & F.a.in_({"", "list"})))
async def t_queue(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await call.answer()
    await show(call, *await queue_view(session))


@router.callback_query(_f("posts"))
async def t_posts(call: CallbackQuery, session: AsyncSession) -> None:
    await call.answer()
    await show(call, *await posts_view(session))


@router.callback_query(_f("pc"))
async def t_claim(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    claim = await session.get(ProfileClaim, callback_data.id, populate_existing=True)
    if claim is None:
        await call.answer(texts.TASK_NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await show(call, *await claim_view(session, claim, s))


@router.callback_query(_f("rp"))
async def t_report(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings) -> None:
    report = await session.get(PromoPostReport, callback_data.id, populate_existing=True)
    view = await report_view(session, report, s) if report is not None else None
    if view is None:
        await call.answer(texts.TASK_NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await show(call, *view)


@router.callback_query(_f("post"))
async def t_post(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                 config: Config) -> None:
    await state.clear()
    view = await post_view(session, callback_data.id, config.admin_level(call.from_user.id))
    if view is None:
        await call.answer(texts.TASK_NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await show(call, *view)


@router.callback_query(_f("file"))
async def t_file(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot) -> None:
    row = await session.get(ProfileClaim if callback_data.v == "pc" else PromoPostReport, callback_data.id)
    if row is None or not row.file_id:
        await call.answer("Скрина нет.", show_alert=True)
        return
    await call.answer()
    what = f"заявка №{row.id}" if callback_data.v == "pc" else f"отчёт №{row.id}"
    await _send_shot(bot, call.from_user.id, row.file_id, row.file_type, f"🖼 Скрин · {what}", None)


async def _after(call: CallbackQuery, view: tuple[str, InlineKeyboardMarkup] | None, alert: str) -> None:
    """Решение принято: у нажатой карточки-скрина подпись уже обновил mark_headers; текстовый экран перерисуем."""
    await call.answer(alert)
    msg = call.message
    if view is not None and isinstance(msg, Message) and msg.text is not None:
        await show(call, *view)


def _self_check(config: Config, actor_id: int, user_id: int) -> bool:
    """Свою заявку проверяет кто-то другой (владельцу можно — он и так распоряжается деньгами)."""
    return actor_id != user_id or actor_id in config.admin_ids


# ------------------------------------------------------------------ «📌 Ссылка в профиле»: начислить
@router.callback_query(_f("pc_ok"))
async def t_claim_ok(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                     bot: Bot, config: Config) -> None:
    claim = await session.get(ProfileClaim, callback_data.id, populate_existing=True)
    if claim is not None and not _self_check(config, call.from_user.id, claim.user_id):
        await call.answer(texts.ADMIN_CANT_SELF, show_alert=True)
        return
    try:
        claim = await promo.approve_profile(session, callback_data.id, call.from_user.id, s, now_utc())
    except promo.PromoError as e:
        await call.answer(plain(str(e)), show_alert=True)
        claim = await session.get(ProfileClaim, callback_data.id, populate_existing=True)
        if claim is not None and claim.status != "pending" and str(e) == texts.TASK_ALREADY_REWARDED:
            marker = texts.TASK_MARK_REJECTED.format(reason=reason_line(claim.reject_reason),
                                                     who=await _stamp(session, call.from_user.id))
            await mark_headers(bot, session, config, KIND_PROFILE, claim.id, marker, done_kb(claim.user_id))
        return
    marker = texts.TASK_MARK_PAID.format(amount=fmt_rub(claim.reward), who=await _stamp(session, call.from_user.id))
    await safe_send(bot, claim.user_id, texts.PROFILE_APPROVED_USER.format(amount=fmt_rub(claim.reward)))
    await mark_headers(bot, session, config, KIND_PROFILE, claim.id, marker, done_kb(claim.user_id))
    await _after(call, await claim_view(session, claim, s, marker), texts.TASK_DONE_ALERT)


# ------------------------------------------------------------------ «📣 Пост о нас»: принять пост / просмотры
@router.callback_query(_f("rp_ok"))
async def t_post_ok(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                    bot: Bot, config: Config) -> None:
    report = await session.get(PromoPostReport, callback_data.id, populate_existing=True)
    post = await session.get(PromoPost, report.post_id) if report is not None else None
    if post is not None and not _self_check(config, call.from_user.id, post.user_id):
        await call.answer(texts.ADMIN_CANT_SELF, show_alert=True)
        return
    try:
        post, report = await promo.approve_post(session, callback_data.id, call.from_user.id, s, now_utc())
    except promo.PromoError as e:
        await call.answer(plain(str(e)), show_alert=True)
        return
    marker = texts.TASK_MARK_ACCEPTED.format(amount=fmt_rub(post.base_reward_paid),
                                             who=await _stamp(session, call.from_user.id))
    await safe_send(bot, post.user_id, texts.POST_ACCEPTED_USER.format(
        amount=fmt_rub(post.base_reward_paid), step=s.post_views_step, per=fmt_rub(int(s.post_views_reward))),
        reply_markup=ik([cb(texts.MY_POSTS_BTN, PromoCb(action="posts"))]))
    await mark_headers(bot, session, config, KIND_PROMO, report.id, marker, done_kb(post.user_id, post.id))
    await _after(call, await report_view(session, report, s, marker), texts.TASK_DONE_ALERT)


async def approve_views(bot: Bot, session: AsyncSession, config: Config, s: RuntimeSettings, report_id: int,
                        views: int, admin_id: int) -> tuple[str, promo.ViewsResult]:
    """Подтвердить просмотры (нарастающим итогом). PromoError — наверх. → (пометка, итог)."""
    res = await promo.approve_views(session, report_id, views, admin_id, s, now_utc())
    post, total = res.post, fmt_rub(res.total)
    if res.pay > 0:
        text = texts.VIEWS_APPROVED_USER.format(views=views, amount=fmt_rub(res.pay), total=total)
    elif res.capped:
        text = texts.VIEWS_CAPPED_USER.format(views=views, total=total)
    else:
        text = texts.VIEWS_APPROVED_ZERO_USER.format(views=views, next=promo.next_views_goal(views, s), total=total)
    await safe_send(bot, post.user_id, text, reply_markup=ik([cb(texts.MY_POSTS_BTN, PromoCb(action="posts"))]))
    marker = texts.TASK_MARK_VIEWS.format(views=views, amount=fmt_rub(res.pay), who=await _stamp(session, admin_id))
    await mark_headers(bot, session, config, KIND_PROMO, report_id, marker, done_kb(post.user_id, post.id))
    return marker, res


async def _views_owner_ok(session: AsyncSession, config: Config, actor_id: int, report_id: int) -> bool:
    report = await session.get(PromoPostReport, report_id, populate_existing=True)
    post = await session.get(PromoPost, report.post_id) if report is not None else None
    return post is None or _self_check(config, actor_id, post.user_id)


@router.callback_query(_f("rv_ok"))
async def t_views_ok(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                     bot: Bot, config: Config) -> None:
    report = await session.get(PromoPostReport, callback_data.id, populate_existing=True)
    if report is None or report.kind != "views":
        await call.answer(texts.TASK_NOT_FOUND, show_alert=True)
        return
    if not await _views_owner_ok(session, config, call.from_user.id, report.id):
        await call.answer(texts.ADMIN_CANT_SELF, show_alert=True)
        return
    try:
        marker, res = await approve_views(bot, session, config, s, report.id, int(report.claimed_views or 0),
                                          call.from_user.id)
    except promo.PromoError as e:
        await call.answer(plain(str(e)), show_alert=True)
        return
    await _after(call, await report_view(session, res.report, s, marker), texts.TASK_DONE_ALERT)


@router.callback_query(_f("rv_num"))
async def t_views_num(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    report = await session.get(PromoPostReport, callback_data.id, populate_existing=True)
    if report is None or report.kind != "views" or report.status != "pending":
        await call.answer(texts.TASK_ALREADY.format(status=texts.REPORT_STATUS.get(report.status, "—"))
                          if report is not None else texts.TASK_NOT_FOUND, show_alert=True)
        return
    post = await session.get(PromoPost, report.post_id)
    await call.answer()
    await ask(call, state, AdminSG.views_number, texts.TASK_VIEWS_ASK.format(prev=post.verified_views if post else 0),
              ik([cancel_btn(T("rp", report.id))]), rid=report.id)


@router.message(AdminSG.views_number)
async def t_views_number(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings,
                         bot: Bot, config: Config) -> None:
    data = await state.get_data()
    rid = int(data.get("rid") or 0)
    report = await session.get(PromoPostReport, rid, populate_existing=True)
    post = await session.get(PromoPost, report.post_id) if report is not None else None
    prev = post.verified_views if post is not None else 0
    raw = (message.text or "").replace(" ", "").replace("\u00a0", "")
    if not raw.isdigit() or int(raw) < prev or int(raw) > promo.MAX_VIEWS:
        await message.answer(texts.TASK_VIEWS_BAD.format(prev=prev))
        return
    if not await _views_owner_ok(session, config, message.from_user.id, rid):
        await state.clear()
        await message.answer(texts.ADMIN_CANT_SELF)
        return
    try:
        marker, res = await approve_views(bot, session, config, s, rid, int(raw), message.from_user.id)
    except promo.PromoError as e:
        await message.answer(str(e))
        if report is None or report.status != "pending":
            await state.clear()
        return
    await state.clear()
    await close_prompt(bot, data)
    view = await report_view(session, res.report, s, marker)
    if view is not None:
        await message.answer(view[0], reply_markup=view[1])


# ------------------------------------------------------------------ отказ (с причиной)
def _reasons_kb(kind: str, rid: int, sub: str) -> InlineKeyboardMarkup:
    codes = REASONS_FOR.get(sub, REASONS_FOR["pc"])
    buttons = [cb(texts.TASK_REJECT_REASONS[c].capitalize(), T("r", rid, f"{kind}.{c}")) for c in codes]
    back = T("pc" if kind == "pc" else "rp", rid)
    return ik(buttons[:2], buttons[2:], [cb(texts.TASK_CUSTOM_REASON_BTN, T("c", rid, kind)),
                                         cb(texts.TASK_NO_REASON_BTN, T("r", rid, f"{kind}.none"))],
              [cb(texts.BTN_BACK, back)])


@router.callback_query(_f("no"))
async def t_reject_ask(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    kind = callback_data.v
    if kind == "pc":
        row = await session.get(ProfileClaim, callback_data.id, populate_existing=True)
        sub, title = "pc", f"📌 Заявка №{callback_data.id}"
    elif kind == "rp":
        row = await session.get(PromoPostReport, callback_data.id, populate_existing=True)
        sub = row.kind if row is not None else "first"
        title = (f"📣 Пост №{row.post_id}" if sub == "first" else f"📈 Отчёт №{row.id} по посту №{row.post_id}") \
            if row is not None else ""
    else:
        await call.answer(texts.STALE_BUTTON)
        return
    if row is None:
        await call.answer(texts.TASK_NOT_FOUND, show_alert=True)
        return
    if row.status != "pending":
        status = (texts.PROFILE_STATUS if kind == "pc" else texts.REPORT_STATUS).get(row.status, row.status)
        await call.answer(texts.TASK_ALREADY.format(status=status), show_alert=True)
        return
    await call.answer()
    await show(call, f"{title}\n{texts.TASK_REJECT_ASK}", _reasons_kb(kind, row.id, sub))


async def do_reject(bot: Bot, session: AsyncSession, config: Config, kind: str, rid: int, reason: str | None,
                    admin_id: int) -> str:
    """Отклонить заявку профиля (pc) или отчёт по посту (rp), уведомить пользователя, пометить карточки."""
    who = await _stamp(session, admin_id)
    marker = texts.TASK_MARK_REJECTED.format(reason=reason_line(reason), who=who)
    if kind == "pc":
        claim = await promo.reject_profile(session, rid, admin_id, reason, now_utc())
        await safe_send(bot, claim.user_id, texts.PROFILE_REJECTED_USER.format(reason=reason_line(reason)),
                        reply_markup=ik([cb(texts.PROFILE_BTN, PromoCb(action="profile"))]))
        await mark_headers(bot, session, config, KIND_PROFILE, claim.id, marker, done_kb(claim.user_id))
        return marker
    post, report = await promo.reject_report(session, rid, admin_id, reason, now_utc())
    url = html.escape(post.post_url)
    if report.kind == "first":
        text = texts.POST_REJECTED_USER.format(url=url, reason=reason_line(reason))
        kb = ik([cb(texts.POST_BTN, PromoCb(action="post"))])
    else:
        text = texts.VIEWS_REJECTED_USER.format(url=url, reason=reason_line(reason))
        kb = ik([cb(texts.MY_POSTS_BTN, PromoCb(action="posts"))])
    await safe_send(bot, post.user_id, text, reply_markup=kb)
    await mark_headers(bot, session, config, KIND_PROMO, report.id, marker, done_kb(post.user_id, post.id))
    return marker


async def _view_of(session: AsyncSession, s: RuntimeSettings, kind: str, rid: int,
                   note: str) -> tuple[str, InlineKeyboardMarkup] | None:
    if kind == "pc":
        claim = await session.get(ProfileClaim, rid, populate_existing=True)
        return await claim_view(session, claim, s, note) if claim else None
    report = await session.get(PromoPostReport, rid, populate_existing=True)
    return await report_view(session, report, s, note) if report else None


@router.callback_query(_f("r"))
async def t_reject(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, s: RuntimeSettings,
                   bot: Bot, config: Config) -> None:
    kind, _, code = callback_data.v.partition(".")
    if kind not in ("pc", "rp") or (code != "none" and code not in texts.TASK_REJECT_REASONS):
        await call.answer(texts.STALE_BUTTON)
        return
    reason = texts.TASK_REJECT_REASONS.get(code)
    try:
        marker = await do_reject(bot, session, config, kind, callback_data.id, reason, call.from_user.id)
    except promo.PromoError as e:
        await call.answer(plain(str(e)), show_alert=True)
        return
    await call.answer(texts.TASK_DONE_ALERT)
    view = await _view_of(session, s, kind, callback_data.id, marker)
    if view is not None:
        await show(call, *view)


@router.callback_query(_f("c"))
async def t_reject_custom(call: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    if callback_data.v not in ("pc", "rp"):
        await call.answer(texts.STALE_BUTTON)
        return
    await call.answer()
    await ask(call, state, AdminSG.task_reason, texts.TASK_REASON_PROMPT,
              ik([cancel_btn(T(callback_data.v, callback_data.id))]), kind=callback_data.v, rid=callback_data.id)


@router.message(AdminSG.task_reason)
async def t_reject_reason(message: Message, state: FSMContext, session: AsyncSession, s: RuntimeSettings,
                          bot: Bot, config: Config) -> None:
    reason = (message.text or "").strip()
    if not reason:
        await message.answer(texts.TASK_REASON_PROMPT)
        return
    data = await state.get_data()
    await state.clear()
    await close_prompt(bot, data)
    kind, rid = data.get("kind", ""), int(data.get("rid") or 0)
    try:
        marker = await do_reject(bot, session, config, kind, rid, reason[:200], message.from_user.id)
    except promo.PromoError as e:
        await message.answer(str(e))
        return
    view = await _view_of(session, s, kind, rid, marker)
    if view is not None:
        await message.answer(view[0], reply_markup=view[1])


# ------------------------------------------------------------------ «🗑 Пост удалён»
@router.callback_query(_f("del"))
async def t_delete_ask(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession) -> None:
    post = await session.get(PromoPost, callback_data.id, populate_existing=True)
    if post is None or post.status not in promo.POST_ACTIVE:
        await call.answer(texts.POST_NOTHING_TO_DELETE.format(status=texts.POST_STATUS.get(post.status, "—"))
                          if post else texts.TASK_NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await show(call, texts.TASK_DELETED_CONFIRM.format(id=post.id),
               ik([cb(texts.TASK_DELETED_GO_BTN, T("del_go", post.id)), cb(texts.BTN_BACK, T("post", post.id))]))


@router.callback_query(_f("del_go"))
async def t_delete(call: CallbackQuery, callback_data: AdminCb, session: AsyncSession, bot: Bot,
                   config: Config) -> None:
    try:
        post, closed = await promo.mark_deleted(session, callback_data.id, call.from_user.id, now_utc())
    except promo.PromoError as e:
        await call.answer(plain(str(e)), show_alert=True)
        return
    marker = texts.TASK_MARK_DELETED.format(who=await _stamp(session, call.from_user.id))
    for rid in closed:
        await mark_headers(bot, session, config, KIND_PROMO, rid, marker, done_kb(post.user_id, post.id))
    await safe_send(bot, post.user_id, texts.POST_DELETED_USER.format(url=html.escape(post.post_url)))
    await call.answer(texts.TASK_DONE_ALERT)
    view = await post_view(session, post.id, config.admin_level(call.from_user.id), marker)
    if view is not None:
        await show(call, *view)


# ------------------------------------------------------------------ ручная корректировка (только 2 уровень)
@router.callback_query(_f("adj"), L2)
async def t_adjust(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession) -> None:
    post = await session.get(PromoPost, callback_data.id, populate_existing=True)
    if post is None:
        await call.answer(texts.TASK_NOT_FOUND, show_alert=True)
        return
    await call.answer()
    await ask(call, state, AdminSG.post_adjust, texts.TASK_ADJUST_PROMPT.format(id=post.id,
                                                                                credited=fmt_rub(post.credited)),
              ik([cancel_btn(T("post", post.id))]), pid=post.id, op=admin_money.new_op())


@router.message(AdminSG.post_adjust, L2)
async def t_adjust_amount(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    data = await state.get_data()
    amount, comment = split_amount(message.text or "")
    post = await session.get(PromoPost, int(data.get("pid") or 0), populate_existing=True)
    if post is None:
        await state.clear()
        await message.answer(texts.TASK_NOT_FOUND)
        return
    if amount is None:
        await message.answer(texts.GIVE_BAD_AMOUNT)
        return
    user = await session.get(User, post.user_id)
    if not admin_money.check_amount(amount, user.balance_rub if user else 0):
        await message.answer(texts.TASK_OVERFLOW)
        return
    await close_prompt(bot, data)
    await state.set_state(None)  # дальше — кнопка подтверждения; данные операции (с кодом op) — в FSM
    await state.update_data(adj={"pid": post.id, "amount": amount, "comment": comment[:300], "op": data.get("op")},
                            prompt_chat=None, prompt_mid=None)
    shown = f"{'+' if amount > 0 else '−'}{fmt_rub(abs(amount))}"
    tail = f"\n💬 {html.escape(comment[:300])}" if comment else ""
    await message.answer(texts.TASK_ADJUST_CONFIRM.format(id=post.id, amount=shown, user=display_name(user),
                                                          comment=tail),
                         reply_markup=ik([cb(texts.GIVE_CONFIRM_BTN, T("adj_go", post.id)),
                                          cancel_btn(T("post", post.id))]))


@router.callback_query(_f("adj_go"), L2)
async def t_adjust_go(call: CallbackQuery, callback_data: AdminCb, state: FSMContext, session: AsyncSession,
                      bot: Bot, config: Config) -> None:
    data = await state.get_data()
    adj = data.get("adj") or {}
    if int(adj.get("pid") or 0) != callback_data.id or not adj.get("op"):
        await call.answer(texts.STALE_BUTTON, show_alert=True)
        return
    try:
        post, applied = await promo.adjust_post(session, callback_data.id, int(adj["amount"]), adj.get("comment"),
                                                call.from_user.id, adj["op"], now_utc())
    except promo.PromoError as e:
        await call.answer(plain(str(e)), show_alert=True)
        return
    await state.update_data(adj=None)
    if applied == 0:
        await call.answer(texts.GIVE_DUPLICATE, show_alert=True)
        return
    shown = f"{'+' if applied > 0 else '−'}{fmt_rub(abs(applied))}"
    comment = adj.get("comment") or ""
    await safe_send(bot, post.user_id, texts.POST_ADJUST_USER.format(
        url=html.escape(post.post_url), amount=shown, comment=f"\n💬 {html.escape(comment)}" if comment else ""))
    note = texts.TASK_ADJUST_DONE.format(id=post.id, amount=shown, credited=fmt_rub(post.credited))
    await call.answer(texts.TASK_DONE_ALERT)
    view = await post_view(session, post.id, config.admin_level(call.from_user.id), note)
    if view is not None:
        await show(call, *view)
