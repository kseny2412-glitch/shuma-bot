"""Прогон диалогов через настоящий диспетчер aiogram с подменённым Telegram API."""
import re
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText,
                             GetMe, SendDocument, SendMessage, SendPhoto)
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
        if isinstance(method, (SendMessage, SendDocument, SendPhoto, EditMessageText)):
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
    return User(id=uid, is_bot=False, first_name="u",
                username="masha_dance" if uid == USER else None)


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
    await press(dp, bot, ADMIN, "kind:create_individual")
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
    await press(dp, bot, ADMIN, "adm:new")
    await press(dp, bot, ADMIN, "kind:individual")
    assert "Свободные окна" in s.texts(ADMIN)[-1]
    await say(dp, bot, ADMIN, "01.12.2099 18:30")
    slot = db.free_slots(offer.id)[0]

    # клиент по ссылке
    await say(dp, bot, USER, f"/start o_{link}")
    slot_button = f"slot:{offer.id}:{slot['id']}"
    assert slot_button in s.buttons()
    await press(dp, bot, USER, slot_button)
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
    assert db.get_order(order_id).slot_id == slot["id"]
    assert not db.free_slots(offer.id)
    assert "01.12.2099 18:30" in pay_msg
    await press(dp, bot, USER, f"paid:{order_id}")
    assert db.get_order(order_id).status == "claimed"
    assert f"adm_ok:{order_id}" in s.buttons()
    await press(dp, bot, ADMIN, f"adm_ok:{order_id}")
    assert db.get_order(order_id).status == "paid"
    assert "вы записаны" in s.texts(USER)[-1]
    card = s.texts(ADMIN)[-1]
    assert "Новый ученик записан" in card and f"tg://user?id={USER}" in card
    assert "@masha_dance" in card and "4 500 ₽" in card
    assert db.set_order_status(order_id, "cancelled")
    assert db.free_slots(offer.id)[0]["id"] == slot["id"]


def test_enrolled_card_without_username_and_html():
    from app.db import Client
    from types import SimpleNamespace
    c = Client(5, "Иванова <Анна>", "01.01.2000", "+79990000000", "a@b.ru", False)
    t = admin.enrolled_card(c, None, SimpleNamespace(title="Курс & ко"), 1000, "Шума")
    assert "&lt;Анна&gt;" in t and "Курс &amp; ко" in t and "ника нет" in t


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


async def test_foreign_contact_rejected(env, db):
    dp, bot, s = env
    offer = db.create_offer("Курс", "course", 1000)
    await press(dp, bot, USER, f"buy:{offer.id}")
    await say(dp, bot, USER, "Петрова Мария")
    await say(dp, bot, USER, "05.03.1995")
    await say(dp, bot, USER, contact=Contact(phone_number="70000000000", first_name="X", user_id=7))
    assert "собственный номер" in s.texts(USER)[-1]


async def test_start_shows_photo_and_menu(env, db):
    dp, bot, s = env
    db.create_offer("Интенсив", "course", 12000)
    db.create_offer("Личное", "individual", 5000, is_public=False)
    await say(dp, bot, USER, "/start")
    assert any(isinstance(c, SendPhoto) for c in s.calls)
    assert s.texts(USER)[-1].startswith("Выберите курс")
    buttons = s.buttons()
    assert len([b for b in buttons if b.startswith("offer:")]) == 1  # личное не показывается


async def test_cancel_frees_seat_and_edit_seats(env, db):
    from app.db import Client
    dp, bot, s = env
    o = db.create_offer("Интенсив", "course", 1000, seats=1)
    db.save_client(Client(USER, "Петрова Мария", "01.01.1990", "+79990000000", "m@x.ru", False))
    oid = db.add_order(USER, o, "+79990000000", "sha", "/tmp/x.pdf", "2026-01-01", "console")
    db.set_order_status(oid, "paid")
    assert db.seats_left(db.get_offer(o.id)) == 0

    await press(dp, bot, ADMIN, f"stud:{o.id}")
    assert any("Петрова Мария" in t for t in s.texts(ADMIN))
    assert f"cnl:{oid}" in s.buttons()
    await press(dp, bot, ADMIN, f"cnl:{oid}")
    assert db.get_order(oid).status == "paid"          # только после подтверждения
    await press(dp, bot, ADMIN, f"cnl_yes:{oid}")
    assert db.get_order(oid).status == "cancelled"
    assert db.seats_left(db.get_offer(o.id)) == 1
    await press(dp, bot, ADMIN, f"cnl_yes:{oid}")      # повторно — ничего не ломается
    assert db.get_order(oid).status == "cancelled"

    await press(dp, bot, ADMIN, f"seats:{o.id}")
    await say(dp, bot, ADMIN, "abc")
    assert "Нужно число" in s.texts(ADMIN)[-1]
    await say(dp, bot, ADMIN, "5")
    assert db.get_offer(o.id).seats == 5 and "свободно: 5" in s.texts(ADMIN)[-1]
    await press(dp, bot, ADMIN, f"seats:{o.id}")
    await say(dp, bot, ADMIN, "-")
    assert db.get_offer(o.id).seats is None


async def test_offline_taken_reduces_free_seats(env, db, tmp_path):
    from app.handlers.client import offer_card
    dp, bot, s = env
    o = db.create_offer("Heels", "course", 8000, seats=20)
    await press(dp, bot, ADMIN, f"offl:{o.id}")
    await say(dp, bot, ADMIN, "пять")
    assert "Нужно число" in s.texts(ADMIN)[-1]
    await say(dp, bot, ADMIN, "5")
    o = db.get_offer(o.id)
    assert o.seats == 20 and db.seats_left(o) == 15
    assert "Мест: 20, свободно: 15 (вне бота записано: 5)" in s.texts(ADMIN)[-1]
    assert "Мест в группе: 20, свободно: 15" in offer_card(db, o)
    db.set_offline_taken(o.id, 25)
    assert db.seats_left(db.get_offer(o.id)) == 0


def test_offline_migration(tmp_path):
    import sqlite3
    from app.db import Database
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE offers (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        kind TEXT NOT NULL, price INTEGER NOT NULL, schedule TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '', seats INTEGER, description TEXT NOT NULL DEFAULT '',
        is_public INTEGER NOT NULL DEFAULT 1, is_active INTEGER NOT NULL DEFAULT 1,
        link_token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""")
    c.execute("INSERT INTO offers (title, kind, price, seats, link_token, created_at) "
              "VALUES ('A','course',100,10,'t','x')")
    c.commit(); c.close()
    db = Database(p)
    o = db.get_offer(1)
    assert o.offline_taken == 0 and db.seats_left(o) == 10


async def test_delete_offer_empty_and_with_orders(env, db):
    from app.db import Client
    dp, bot, s = env
    empty = db.create_offer("Пустой", "course", 100)
    used = db.create_offer("Прошедший", "course", 200)
    db.save_client(Client(USER, "Петрова Мария", "01.01.1990", "+79990000000", "m@x.ru", False))
    oid = db.add_order(USER, used, "+79990000000", "sha", "/tmp/x.pdf", "2026-01-01", "console")

    await press(dp, bot, ADMIN, f"del:{empty.id}")
    assert db.get_offer(empty.id) is not None            # без подтверждения не удаляем
    await press(dp, bot, ADMIN, f"del_yes:{empty.id}")
    assert db.get_offer(empty.id) is None                # заявок не было — удалён совсем

    await press(dp, bot, ADMIN, f"del_yes:{used.id}")
    o = db.get_offer(used.id)
    assert o.is_archived and not o.is_active            # есть заявка — в архив
    assert db.get_order(oid) is not None
    assert used.id not in [x.id for x in db.list_offers()]
    assert any(r["title"] == "Прошедший" for r in db.orders_report())   # CSV сохраняет
    await press(dp, bot, ADMIN, f"del_yes:{used.id}")    # повторно — без ошибок


async def test_reorder_offers_and_menu_without_price(env, db):
    dp, bot, s = env
    a = db.create_offer("Первый", "course", 1000)
    b = db.create_offer("Второй", "course", 2000)
    c = db.create_offer("Третий", "course", 3000)
    assert [o.title for o in db.list_offers()] == ["Третий", "Второй", "Первый"]  # новые сверху
    await press(dp, bot, ADMIN, f"down:{c.id}")
    assert [o.title for o in db.list_offers()] == ["Второй", "Третий", "Первый"]
    await press(dp, bot, ADMIN, f"up:{a.id}")
    assert [o.title for o in db.list_offers(public_only=True)] == ["Второй", "Первый", "Третий"]
    assert db.move_offer(b.id, -1) == (1, 3)                       # край — остаётся на месте
    await say(dp, bot, USER, "/start")
    menu = [c for c in s.calls if getattr(c, "text", None) == "Выберите курс или занятие:"][-1]
    labels = [row[0].text for row in menu.reply_markup.inline_keyboard]
    assert labels == ["Второй", "Первый", "Третий"]


async def test_admin_sets_contract_start(env, db):
    dp, bot, s = env
    await press(dp, bot, ADMIN, "adm:num")
    assert "№ 1" in s.texts(ADMIN)[-1]
    await say(dp, bot, ADMIN, "abc")
    assert "Нужно число" in s.texts(ADMIN)[-1]
    await say(dp, bot, ADMIN, "/admin")
    assert s.texts(ADMIN)[-1] == "Админка:"
    assert db.next_contract_number() == 1
    await press(dp, bot, ADMIN, "adm:num")
    await say(dp, bot, ADMIN, "25")
    assert "№ 25" in s.texts(ADMIN)[-1] and db.next_contract_number() == 25
