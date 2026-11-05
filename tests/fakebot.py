"""Фейковый Telegram для сквозных тестов: бот работает по-настоящему, запросы к API записываются."""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
import types
import typing
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatFullInfo,
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Contact,
    File,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageId,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
)
from aiogram.types import User as TgUser

from app.bot import files

_ids = itertools.count(1000)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple[TelegramMethod, Any]] = []
        self.files: dict[str, bytes] = {}
        self.blocked: set[int] = set()  # эти пользователи «заблокировали бота» — отправка падает с 403
        self.channel_members: set[int] = set()  # кто подписан на канал (ответ getChatMember)
        self.channel_error: str | None = None  # «бот не админ канала» — getChatMember падает с 400
        self.channel_admins: set[int] = set()  # администраторы канала (для проверки прав бота при запуске)
        self.bios: dict[int, str] = {}  # «О себе» в профиле (ответ getChat) — автопроверка задания «профиль»
        self.getchat_error: str | None = None  # getChat падает с 400 — «не удалось проверить»

    async def close(self) -> None:
        return None

    def _message(self, bot: Bot, method: TelegramMethod, message_id: int | None = None) -> Message:
        chat_id = getattr(method, "chat_id", None)
        data: dict[str, Any] = {
            "message_id": message_id or next(_ids), "date": int(time.time()),
            "chat": {"id": int(chat_id) if isinstance(chat_id, int | str) and str(chat_id).lstrip("-").isdigit() else 0,
                     "type": "private"},
            "from": {"id": bot.id, "is_bot": True, "first_name": "Аегис", "username": "aegis_test_bot"},
        }
        for field in ("text", "caption"):
            value = getattr(method, field, None)
            if isinstance(value, str):
                data[field] = value
        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            data["reply_markup"] = markup.model_dump(exclude_none=True)
        if getattr(method, "document", None) is not None:
            n = next(_ids)
            data["document"] = {"file_id": f"botdoc{n}", "file_unique_id": f"ubotdoc{n}"}
        return Message.model_validate(data, context={"bot": bot})

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        chat = getattr(method, "chat_id", None)
        if chat is not None and str(chat) in {str(b) for b in self.blocked}:
            self.log.append((method, None))
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        rt = method.__returning__
        origin, args = typing.get_origin(rt), typing.get_args(rt)
        if type(method).__name__ == "GetChat":
            if self.getchat_error:
                self.log.append((method, None))
                raise TelegramBadRequest(method=method, message=self.getchat_error)
            gifts = dict.fromkeys(("unlimited_gifts", "limited_gifts", "unique_gifts", "premium_subscription",
                                   "gifts_from_channels"), False)
            result = ChatFullInfo.model_validate({"id": int(method.chat_id), "type": "private", "accent_color_id": 0,
                                                  "max_reaction_count": 0, "accepted_gift_types": gifts,
                                                  "bio": self.bios.get(int(method.chat_id))})
            self.log.append((method, result))
            return result
        if type(method).__name__ == "GetChatMember":
            if self.channel_error:
                self.log.append((method, None))
                raise TelegramBadRequest(method=method, message=self.channel_error)
            who = TgUser(id=method.user_id, is_bot=False, first_name="u")
            if method.user_id in self.channel_admins:
                rights = {f: False for f, info in ChatMemberAdministrator.model_fields.items()
                          if info.is_required() and f != "user"}
                result: Any = ChatMemberAdministrator(user=who, **rights)
            else:
                result = (ChatMemberMember(user=who) if method.user_id in self.channel_members
                          else ChatMemberLeft(user=who))
        elif rt is bool:
            result = True
        elif rt is Message or (origin in (typing.Union, types.UnionType) and Message in args):
            result = self._message(bot, method, getattr(method, "message_id", None) if rt is not Message else None)
        elif origin is list and args and args[0] is Message:
            result = [self._message(bot, method) for _ in getattr(method, "media", [])]
        elif origin is list and args and args[0] is Update:  # getUpdates при long polling: апдейтов нет
            await asyncio.sleep(0.05)
            result = []
        elif rt is MessageId:
            result = MessageId(message_id=next(_ids))
        elif origin is list and args and args[0] is MessageId:  # copyMessages: альбом целиком
            result = [MessageId(message_id=next(_ids)) for _ in getattr(method, "message_ids", [])]
        elif rt is File:
            fid = method.file_id
            result = File(file_id=fid, file_unique_id="u" + fid, file_path=f"files/{fid}",
                          file_size=len(self.files.get(fid, b"x")))
        elif rt is TgUser:
            result = TgUser(id=bot.id, is_bot=True, first_name="Аегис", username="aegis_test_bot")
        elif rt is str:
            result = "https://t.me/$fake"
        else:
            result = True
        self.log.append((method, result))
        return result

    async def stream_content(self, url: str, headers: dict | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True):  # noqa: ANN201
        for fid, content in self.files.items():
            if url.endswith(f"files/{fid}"):
                yield content
                return
        yield b"\xff\xd8\xff\xe0fake-jpeg"


def make_bot() -> tuple[Bot, FakeSession]:
    fake = FakeSession()
    bot = Bot("123456:TEST-TOKEN", session=fake, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, fake


class _ErrorCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class Harness:
    """Отправляет боту апдейты от имени пользователей и смотрит, что он ответил.

    После каждого апдейта проверяет, что в логах нет ошибок (исключение в хендлере ловит router.errors
    и пишет log.error — тест должен это увидеть, а не пройти молча).
    """

    def __init__(self, dp: Dispatcher, bot: Bot, fake: FakeSession) -> None:
        self.dp, self.bot, self.fake = dp, bot, fake
        self.users: dict[int, TgUser] = {}
        self._update = itertools.count(1)
        self.errors = _ErrorCollector()
        logging.getLogger().addHandler(self.errors)

    def close(self) -> None:
        logging.getLogger().removeHandler(self.errors)

    def assert_no_errors(self) -> None:
        if self.errors.records:
            details = "\n".join(f"{r.name}: {r.getMessage()}\n{logging.Formatter().formatException(r.exc_info)}"
                                if r.exc_info else f"{r.name}: {r.getMessage()}" for r in self.errors.records)
            self.errors.records.clear()
            raise AssertionError("бот залогировал ошибку:\n" + details)

    def user(self, uid: int, name: str, *, username: str | None = None) -> TgUser:
        self.users[uid] = TgUser(id=uid, is_bot=False, first_name=name, username=username or f"user{uid}",
                                 language_code="ru")
        return self.users[uid]

    # ---- входящие события
    async def _feed(self, **kw: Any) -> None:
        await self.dp.feed_update(self.bot, Update(update_id=next(self._update), **kw))
        await files.flush_acks()
        self.assert_no_errors()

    MIME = {".html": "text/html", ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

    def build(self, uid: int, text: str | None = None, *, photo: str | None = None,
              document: tuple[str, str, bytes] | None = None, contact: int | None = None,
              reply_to: Message | None = None, media_group: str | None = None, caption: str | None = None,
              voice: str | None = None, video: str | None = None, sticker: str | None = None,
              audio: str | None = None, message_id: int | None = None) -> Message:
        """Сообщение от пользователя uid (не отправляя его боту) — для альбомов и проверок."""
        data: dict[str, Any] = {"message_id": message_id or next(_ids), "date": int(time.time()),
                                "chat": Chat(id=uid, type="private"), "from_user": self.users[uid]}
        if text is not None:
            data["text"] = text
        if caption is not None:
            data["caption"] = caption
        if voice is not None:
            data["voice"] = {"file_id": voice, "file_unique_id": "u" + voice, "duration": 3}
        if audio is not None:
            data["audio"] = {"file_id": audio, "file_unique_id": "u" + audio, "duration": 3}
        if video is not None:
            data["video"] = {"file_id": video, "file_unique_id": "u" + video, "width": 640, "height": 480,
                             "duration": 5}
        if sticker is not None:
            data["sticker"] = {"file_id": sticker, "file_unique_id": "u" + sticker, "type": "regular",
                               "width": 512, "height": 512, "is_animated": False, "is_video": False}
        if contact is not None:
            data["contact"] = Contact(phone_number="79991234567", first_name="Я", user_id=contact)
        if reply_to is not None:
            data["reply_to_message"] = reply_to
        if media_group is not None:
            data["media_group_id"] = media_group
        if photo is not None:
            self.fake.files[photo] = b"\xff\xd8\xff\xe0" + photo.encode()
            data["photo"] = [{"file_id": photo, "file_unique_id": "u" + photo, "width": 800, "height": 600,
                              "file_size": 2048}]
        if document is not None:
            fid, name, content = document
            self.fake.files[fid] = content
            ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
            data["document"] = {"file_id": fid, "file_unique_id": "u" + fid, "file_name": name,
                                "mime_type": self.MIME.get(ext, "application/octet-stream"),
                                "file_size": len(content)}
        return Message(**data)

    async def send(self, uid: int, text: str | None = None, **kw: Any) -> None:
        """contact — id владельца контакта (кнопка «отправить мой номер» → свой id); reply_to — ответ свайпом;
        caption, voice, video, sticker, audio — другие типы сообщений (v2.3: чат админа с клиентом)."""
        await self._feed(message=self.build(uid, text, **kw))

    async def send_message(self, message: Message) -> None:
        await self._feed(message=message)

    async def send_album(self, uid: int, photos: list[str], *, group: str, caption: str | None = None,
                         reply_to: Message | None = None) -> list[Message]:
        """Альбом, как его шлёт Telegram: по сообщению на фото с общим media_group_id, почти одновременно.
        Подтверждения (debounce) не сбрасываем между фото — только после последнего, как в жизни."""
        msgs = [self.build(uid, photo=p, media_group=group, caption=caption if i == 0 else None,
                           reply_to=reply_to if i == 0 else None) for i, p in enumerate(photos)]
        old = files.ACK_DELAY
        files.ACK_DELAY = max(old, 0.3)
        try:
            for m in msgs:
                await self.dp.feed_update(self.bot, Update(update_id=next(self._update), message=m))
        finally:
            files.ACK_DELAY = old
        await files.flush_acks()
        self.assert_no_errors()
        return msgs

    async def pre_checkout(self, uid: int, payload: str, amount: int, currency: str = "XTR") -> bool:
        """Telegram спрашивает «можно ли принять оплату» — возвращает ответ бота (ok)."""
        qid = str(next(_ids))
        await self._feed(pre_checkout_query=PreCheckoutQuery(id=qid, from_user=self.users[uid], currency=currency,
                                                             total_amount=amount, invoice_payload=payload))
        answers = [m for m, _ in self.fake.log
                   if type(m).__name__ == "AnswerPreCheckoutQuery" and m.pre_checkout_query_id == qid]
        assert len(answers) == 1, "бот не ответил на pre_checkout_query"
        return bool(answers[0].ok)

    async def successful_payment(self, uid: int, payload: str, amount: int, charge_id: str,
                                 currency: str = "XTR") -> None:
        sp = SuccessfulPayment(currency=currency, total_amount=amount, invoice_payload=payload,
                               telegram_payment_charge_id=charge_id, provider_payment_charge_id="")
        await self._feed(message=Message(message_id=next(_ids), date=int(time.time()),
                                         chat=Chat(id=uid, type="private"), from_user=self.users[uid],
                                         successful_payment=sp))

    async def block_bot(self, uid: int, blocked: bool = True) -> None:
        """Пользователь блокирует (или разблокирует) бота: Telegram шлёт my_chat_member, отправка — 403."""
        me = TgUser(id=self.bot.id, is_bot=True, first_name="Аегис", username="aegis_test_bot")
        member, banned = ChatMemberMember(user=me), ChatMemberBanned(user=me, until_date=0)
        old, new = (member, banned) if blocked else (banned, member)
        (self.fake.blocked.add if blocked else self.fake.blocked.discard)(uid)
        await self._feed(my_chat_member=ChatMemberUpdated(chat=Chat(id=uid, type="private"), from_user=self.users[uid],
                                                          date=int(time.time()), old_chat_member=old,
                                                          new_chat_member=new))

    async def channel(self, uid: int, joined: bool, *, username: str = "aegis_samara") -> None:
        """Пользователь подписался на канал / отписался: Telegram шлёт chat_member (бот — админ канала)."""
        user = self.users[uid]
        (self.fake.channel_members.add if joined else self.fake.channel_members.discard)(uid)
        member, left = ChatMemberMember(user=user), ChatMemberLeft(user=user)
        old, new = (left, member) if joined else (member, left)
        await self._feed(chat_member=ChatMemberUpdated(chat=Chat(id=-100777, type="channel", username=username),
                                                       from_user=user, date=int(time.time()),
                                                       old_chat_member=old, new_chat_member=new))

    def last_message(self, uid: int) -> Message:
        """Последнее сообщение бота в чате uid — например, чтобы ответить на него свайпом."""
        method, msg = self.messages(uid)[-1]
        return msg

    def calls(self, method_name: str) -> list[TelegramMethod]:
        return [m for m, _ in self.fake.log if type(m).__name__ == method_name]

    async def click(self, uid: int, label: str, *, exact: bool = False) -> InlineKeyboardButton:
        """Нажимает последнюю кнопку в чате uid, текст которой содержит label (или равен ему при exact)."""
        msg, btn = self.find_button(uid, label, exact=exact)
        await self.press(uid, msg, btn)
        return btn

    async def click_data(self, uid: int, data: str) -> InlineKeyboardButton:
        """Нажимает последнюю на экране кнопку с этим callback_data (когда текст кнопки неуникален)."""
        for msg, markup in reversed(self.screen(uid)):
            for row in (markup.inline_keyboard if markup else []):
                for btn in row:
                    if btn.callback_data == data:
                        await self.press(uid, msg, btn)
                        return btn
        raise AssertionError(f"кнопка с данными {data!r} не найдена у {uid}")

    async def press(self, uid: int, msg: Message, btn: InlineKeyboardButton) -> None:
        assert btn.callback_data, f"кнопка «{btn.text}» без callback_data"
        await self._feed(callback_query=CallbackQuery(id=str(next(_ids)), from_user=self.users[uid],
                                                      chat_instance="ci", data=btn.callback_data, message=msg))

    def _unseen_button(self, uid: int, seen: set[str],
                       skip: tuple[str, ...]) -> tuple[Message, InlineKeyboardButton] | None:
        # Берём и старые версии перерисованных сообщений: так обход доходит до всех экранов,
        # даже если меню перерисовалось в подменю на месте.
        for method, msg in reversed(self.messages(uid)):
            markup = getattr(method, "reply_markup", None)
            if not isinstance(markup, InlineKeyboardMarkup):
                continue
            for row in markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data and btn.callback_data not in seen and not any(s in btn.text for s in skip):
                        return msg.model_copy(update={"reply_markup": markup}), btn
        return None

    async def crawl(self, uid: int, *, entry: str | None = None, skip: tuple[str, ...] = (),
                    limit: int = 500) -> list[str]:
        """Нажимает по разу каждую callback-кнопку, которая появляется на экране uid (новые — первыми).

        Кнопки берутся из всех версий сообщений (и до перерисовки); когда новых не осталось — отправляется
        entry (например, /admin), и обход продолжается с нового меню.
        Дымовой тест: любое исключение в хендлере всплывёт через assert_no_errors. Возвращает тексты кнопок.
        """
        seen: set[str] = set()
        pressed: list[str] = []
        while len(pressed) < limit:
            target = self._unseen_button(uid, seen, skip)
            if target is None and entry is not None:
                await self.send(uid, entry)
                target = self._unseen_button(uid, seen, skip)
            if target is None:
                break
            msg, btn = target
            seen.add(btn.callback_data)
            pressed.append(btn.text)
            await self.press(uid, msg, btn)
        return pressed

    # ---- что бот отправил
    def messages(self, uid: int) -> list[tuple[TelegramMethod, Message]]:
        """Всё, что появилось в чате uid: отправленные, отредактированные и скопированные сообщения."""
        out = []
        for method, result in self.fake.log:
            chat = getattr(method, "chat_id", None)
            if chat is None or str(chat) != str(uid):
                continue
            if isinstance(result, Message):
                out.append((method, result))
            elif isinstance(result, MessageId):  # copyMessage: в чате появилось сообщение с нашими кнопками
                out.append((method, self.fake._message(self.bot, method, result.message_id)))
        return out

    def texts(self, uid: int) -> list[str]:
        res = []
        for method, _ in self.messages(uid):
            t = getattr(method, "text", None) or getattr(method, "caption", None)
            if isinstance(t, str):
                res.append(t)
        return res

    def last_text(self, uid: int) -> str:
        return self.texts(uid)[-1]

    def screen(self, uid: int) -> list[tuple[Message, InlineKeyboardMarkup | None]]:
        """Чат uid «как на экране»: по одному актуальному состоянию на сообщение, последние изменённые — в конце.

        Правки (editMessageText/ReplyMarkup) заменяют старую версию, поэтому убранные кнопки нажать нельзя.
        """
        view: dict[int, tuple[Message, InlineKeyboardMarkup | None]] = {}
        for method, msg in self.messages(uid):
            markup = getattr(method, "reply_markup", None)
            markup = markup if isinstance(markup, InlineKeyboardMarkup) else None
            name = type(method).__name__
            if name.startswith("EditMessage"):
                mid = int(getattr(method, "message_id", 0) or 0)
                old = view.pop(mid, None)
                if name == "EditMessageReplyMarkup" and old is not None:
                    msg = old[0].model_copy(update={"reply_markup": markup})
                view[mid] = (msg, markup)
            else:
                view[msg.message_id] = (msg, markup)
        return list(view.values())

    def find_button(self, uid: int, label: str, *, exact: bool = False) -> tuple[Message, InlineKeyboardButton]:
        for msg, markup in reversed(self.screen(uid)):
            if markup is None:
                continue
            for row in markup.inline_keyboard:
                for btn in row:
                    if (btn.text == label) if exact else (label in btn.text):
                        return msg, btn
        raise AssertionError(f"кнопка «{label}» не найдена у {uid}. Последние тексты: {self.texts(uid)[-3:]}")

    def alerts(self) -> list[str]:
        return [m.text for m, _ in self.fake.log if type(m).__name__ == "AnswerCallbackQuery" and m.text]

    def sent_documents(self, uid: int) -> list[TelegramMethod]:
        return [m for m, _ in self.fake.log if type(m).__name__ == "SendDocument" and str(m.chat_id) == str(uid)]
