"""Прогон диалогов через настоящий диспетчер aiogram с подменённым Telegram API."""
import re
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText,
                             GetMe, SendDocument, SendMessage)
from aiogram.types import CallbackQuery, Chat, Contact, Message, Update, User

from app.handlers import admin, client
from app.sms import ConsoleSender

ADMIN, USER = 1, 42


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=999, is_bot=True, first_name="bot", username="shuma_test_bot")
        if isinstance(method, (SendMessage, SendDocument, EditMessageText)):
            return Message(message_id=len(self.calls), date=datetime.now(),
                           chat=Chat(id=getattr(method, "chat_id", 0) or 0, type="private"),
                           text=getattr(method, "text", None) or "doc")
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    def texts(self, chat_id=None):
        return [c.text for c in self.calls if isinstance(c, SendMessage)
                and (chat_id is None or c.chat_id == chat_id)]

    def buttons(self):
        out = []
        for c in self.calls:
            rm = getattr(c, "reply_markup", None)
            for row in getattr(rm, "inline_keyboard", None) or []:
                out += [b.callback_data for b in row]
        return out


@pytest.fixture
def env(db, cfg):
    from dataclasses import replace
    cfg = replace(cfg, admin_ids=frozenset({ADMIN}))
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    dp = Dispatcher(storage=MemoryStorage(), db=db, cfg=cfg, sms=ConsoleSender())
    dp.include_router(admin.router)
    dp.include_router(client.router)
    # роутеры — модульные синглтоны; отвязываем после теста
    yield dp, bot, session
    admin.router._parent_router = None
    client.router._parent_router = None


_uid = iter(range(1, 10**6))


def user(uid):
    return User(id=uid, is_bot=False, first_name="u")


async def say(dp, bot, uid, text=None, contact=None):
    m = Message(message_id=next(_uid), date=datetime.now(), chat=Chat(id=uid, type="private"),
                from_user=user(uid), text=text, contact=contact)
    await dp.feed_update(bot, Update(update_id=next(_uid), message=m))


async def press(dp, bot, uid, data):
    m = Message(message_id=next(_uid), date=datetime.now(), chat=Chat(id=uid, type="private"),
                from_user=user(999), text="x")
    cb = CallbackQuery(id=str(next(_uid)), from_user=user(uid), chat_instance="c", data=data,
                       message=m)
    await dp.feed_update(bot, Update(update_id=next(_uid), callback_query=cb))


async def test_full_path(env, db):
    dp, bot, s = env
    # админ создаёт личное занятие
    await say(dp, bot, ADMIN, "/admin")
    await press(dp, bot, ADMIN, "adm:new")
    await press(dp, bot, ADMIN, "kind:individual")
    await say(dp, bot, ADMIN, "Индивидуальная тренировка")
    await say(dp, bot, ADMIN, "4 500")
    await say(dp, bot, ADMIN, "-")
    await say(dp, bot, ADMIN, "-")
    await say(dp, bot, ADMIN, "-")
    await say(dp, bot, ADMIN, "-")
    await press(dp, bot, ADMIN, "vis:private")
    link = re.search(r"start=o_(\S+)", s.texts(ADMIN)[-1]).group(1)
    offer = db.get_offer_by_token(link)
    assert offer.price == 4500 and not offer.is_public

    # клиент по ссылке
    await say(dp, bot, USER, f"/start o_{link}")
    await press(dp, bot, USER, f"buy:{offer.id}")
    await say(dp, bot, USER, "Петрова Мария Ивановна")
    await say(dp, bot, USER, "05.03.1995")
    await say(dp, bot, USER, contact=Contact(phone_number="79990001122", first_name="M", user_id=USER))
    await say(dp, bot, USER, "m@example.com")
    await press(dp, bot, USER, "consent:yes")
    await press(dp, bot, USER, "photo:no")
    assert sum(isinstance(c, SendDocument) for c in s.calls) == 2  # договор + заявка
    await press(dp, bot, USER, "sign")
    code = re.search(r"Код: (\d{6})", s.texts(USER)[-1]).group(1)
    await say(dp, bot, USER, "123")  # неверный
    assert "неверный" in s.texts(USER)[-1]
    await say(dp, bot, USER, code)
    admin_docs = [c for c in s.calls if isinstance(c, SendDocument) and c.chat_id == ADMIN]
    assert len(admin_docs) == 2  # админ получил подписанные договор и заявку
    pay_msg = s.texts(USER)[-1]
    assert "https://shumkadance.tb.ru/" in pay_msg and "<b>4500</b>" in pay_msg
    order_id = db.conn.execute("SELECT id FROM orders").fetchone()[0]
    await press(dp, bot, USER, f"paid:{order_id}")
    assert db.get_order(order_id).status == "claimed"
    assert f"adm_ok:{order_id}" in s.buttons()
    await press(dp, bot, ADMIN, f"adm_ok:{order_id}")
    assert db.get_order(order_id).status == "paid"
    assert "вы записаны" in s.texts(USER)[-1]


async def test_non_admin_cannot_use_admin(env, db):
    dp, bot, s = env
    await press(dp, bot, USER, "adm:new")
    await say(dp, bot, USER, "/admin")
    assert not any("Что создаём" in t for t in s.texts())
    o = db.create_offer("К", "course", 100)
    cid = db.get_client(USER)
    assert cid is None
    await press(dp, bot, USER, "adm_ok:1")
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


async def test_foreign_contact_rejected(env):
    dp, bot, s = env
    await press(dp, bot, USER, "buy:1")
    await say(dp, bot, USER, "Петрова Мария")
    await say(dp, bot, USER, "05.03.1995")
    await say(dp, bot, USER, contact=Contact(phone_number="70000000000", first_name="X", user_id=7))
    assert "собственный номер" in s.texts(USER)[-1]
