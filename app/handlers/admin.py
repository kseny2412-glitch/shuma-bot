"""Админка для Шумы и Ксени: курсы с любой ценой, личные ссылки, подтверждение оплат, выгрузка."""
from __future__ import annotations

import csv
import io
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message, TelegramObject

from ..config import Config
from ..db import Database
from ..services import fmt_price
from .client import kb

router = Router(name="admin")
log = logging.getLogger(__name__)


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, cfg: Config) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in cfg.admin_ids)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class NewOffer(StatesGroup):
    kind = State()
    title = State()
    price = State()
    schedule = State()
    address = State()
    seats = State()
    description = State()
    visibility = State()


SKIP = "-"
MENU = kb([("➕ Новый курс / занятие", "adm:new")],
          [("📋 Все предложения", "adm:list")],
          [("💳 Ждут подтверждения оплаты", "adm:pending")],
          [("📥 Выгрузка заказов (CSV)", "adm:csv")])


@router.message(Command("admin"))
async def admin_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Админка:", reply_markup=MENU)


def offer_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start=o_{token}"


# ---------- создание предложения ----------

@router.callback_query(F.data == "adm:new")
async def new_offer(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(NewOffer.kind)
    await cb.message.answer("Что создаём?", reply_markup=kb(
        [("Групповой курс", "kind:course")], [("Индивидуальное занятие", "kind:individual")]))


@router.callback_query(NewOffer.kind, F.data.startswith("kind:"))
async def nf_kind(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(kind=cb.data.split(":")[1])
    await state.set_state(NewOffer.title)
    await cb.message.answer("Название (например: Интенсив «Бачата», 5 дней):")


@router.message(NewOffer.title, F.text)
async def nf_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip())
    await state.set_state(NewOffer.price)
    await msg.answer("Цена в рублях, только число (например 12000):")


@router.message(NewOffer.price, F.text)
async def nf_price(msg: Message, state: FSMContext):
    digits = msg.text.replace(" ", "").replace("₽", "")
    if not digits.isdigit() or int(digits) <= 0:
        return await msg.answer("Нужно целое число больше нуля, например 12000.")
    await state.update_data(price=int(digits))
    await state.set_state(NewOffer.schedule)
    await msg.answer(f"Даты и время (например: 1–5 декабря, 19:00–21:00). «{SKIP}» — пропустить:")


def _opt(text: str) -> str:
    t = text.strip()
    return "" if t == SKIP else t


@router.message(NewOffer.schedule, F.text)
async def nf_schedule(msg: Message, state: FSMContext):
    await state.update_data(schedule=_opt(msg.text))
    await state.set_state(NewOffer.address)
    await msg.answer(f"Адрес. «{SKIP}» — пропустить:")


@router.message(NewOffer.address, F.text)
async def nf_address(msg: Message, state: FSMContext):
    await state.update_data(address=_opt(msg.text))
    await state.set_state(NewOffer.seats)
    await msg.answer(f"Сколько мест? Число, или «{SKIP}» — без ограничения:")


@router.message(NewOffer.seats, F.text)
async def nf_seats(msg: Message, state: FSMContext):
    t = _opt(msg.text)
    if t and not t.isdigit():
        return await msg.answer(f"Нужно число или «{SKIP}».")
    await state.update_data(seats=int(t) if t else None)
    await state.set_state(NewOffer.description)
    await msg.answer(f"Короткое описание для клиентов. «{SKIP}» — пропустить:")


@router.message(NewOffer.description, F.text)
async def nf_description(msg: Message, state: FSMContext):
    await state.update_data(description=_opt(msg.text))
    await state.set_state(NewOffer.visibility)
    await msg.answer("Где показывать?", reply_markup=kb(
        [("В общем меню бота", "vis:public")],
        [("Только по личной ссылке", "vis:private")]))


@router.callback_query(NewOffer.visibility, F.data.startswith("vis:"))
async def nf_visibility(cb: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    await cb.answer()
    d = await state.get_data()
    await state.clear()
    offer = db.create_offer(d["title"], d["kind"], d["price"], d["schedule"], d["address"],
                            d["seats"], d["description"], is_public=cb.data.endswith("public"))
    me = await bot.get_me()
    where = "в меню бота и по ссылке" if offer.is_public else "только по ссылке"
    await cb.message.answer(
        f"Готово: <b>{offer.title}</b>, {fmt_price(offer.price)} ₽ ({where}).\n\n"
        f"Ссылка для клиента:\n{offer_link(me.username, offer.link_token)}",
        disable_web_page_preview=True)


# ---------- список ----------

@router.callback_query(F.data == "adm:list")
async def list_offers(cb: CallbackQuery, db: Database, bot: Bot):
    await cb.answer()
    offers = db.list_offers()
    if not offers:
        return await cb.message.answer("Пока ничего нет.")
    me = await bot.get_me()
    for o in offers[:30]:
        status = "активно" if o.is_active else "скрыто"
        vis = "в меню" if o.is_public else "по ссылке"
        left = db.seats_left(o)
        seats = f", мест осталось: {left}" if left is not None else ""
        btn = ("Скрыть", f"hide:{o.id}") if o.is_active else ("Показать", f"show:{o.id}")
        await cb.message.answer(
            f"<b>{o.title}</b> — {fmt_price(o.price)} ₽\n{o.kind_label}, {vis}, {status}{seats}\n"
            f"{offer_link(me.username, o.link_token)}",
            reply_markup=kb([btn]), disable_web_page_preview=True)


@router.callback_query(F.data.startswith(("hide:", "show:")))
async def toggle(cb: CallbackQuery, db: Database):
    action, oid = cb.data.split(":")
    db.set_offer_active(int(oid), action == "show")
    await cb.answer("Скрыто" if action == "hide" else "Снова доступно")
    await cb.message.edit_reply_markup(reply_markup=kb(
        [("Показать", f"show:{oid}")] if action == "hide" else [("Скрыть", f"hide:{oid}")]))


# ---------- оплаты ----------

@router.callback_query(F.data == "adm:pending")
async def pending(cb: CallbackQuery, db: Database):
    await cb.answer()
    rows = db.pending_orders()
    if not rows:
        return await cb.message.answer("Нет оплат, ждущих подтверждения.")
    for r in rows:
        await cb.message.answer(
            f"{r['full_name']} ({r['phone']}): «{r['title']}», {fmt_price(r['amount'])} ₽",
            reply_markup=kb([("✅ Оплата получена", f"adm_ok:{r['id']}"),
                             ("❌ Не пришла", f"adm_no:{r['id']}")]))


@router.callback_query(F.data.startswith(("adm_ok:", "adm_no:")))
async def decide(cb: CallbackQuery, db: Database, cfg: Config, bot: Bot):
    action, oid = cb.data.split(":")
    order = db.get_order(int(oid))
    status = "paid" if action == "adm_ok" else "rejected"
    if not order or not db.set_order_status(order.id, status, by=cb.from_user.id):
        await cb.answer("Уже решено другим админом", show_alert=True)
        return await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    offer = db.get_offer(order.offer_id)
    await cb.message.edit_text(cb.message.html_text + (
        "\n\n✅ Подтверждено" if status == "paid" else "\n\n❌ Отклонено"))
    if status == "paid":
        text = f"🎉 Оплата за «{offer.title}» получена, вы записаны! До встречи на занятиях."
    else:
        text = (f"Мы не нашли оплату за «{offer.title}». Проверьте, пожалуйста, платёж "
                f"или напишите Шуме.")
    await bot.send_message(order.tg_id, text)
    if status == "paid":
        await notify_enrolled(db, cfg, bot, order, offer, confirmed_by=cb.from_user)


def enrolled_card(client, username, offer, amount, confirmed_by_name) -> str:
    """Карточка нового ученика для админов: ник ссылкой и данные анкеты."""
    name_link = f'<a href="tg://user?id={client.tg_id}">{escape(client.full_name)}</a>'
    nick = f"@{escape(username)}" if username else "ника нет (откройте чат по имени выше)"
    return (f"🎉 <b>Новый ученик записан</b>\n\n"
            f"👤 {name_link}\n"
            f"Telegram: {nick}\n"
            f"📞 {escape(client.phone)}\n"
            f"✉️ {escape(client.email)}\n"
            f"🎂 {escape(client.birth_date)}\n\n"
            f"Курс: «{escape(offer.title)}»\n"
            f"Оплачено: {fmt_price(amount)} ₽\n"
            f"Подтвердил(а): {escape(confirmed_by_name)}")


async def notify_enrolled(db: Database, cfg: Config, bot: Bot, order, offer, confirmed_by):
    client = db.get_client(order.tg_id)
    text = enrolled_card(client, db.get_username(order.tg_id), offer, order.amount,
                         confirmed_by.full_name)
    for admin in cfg.admin_ids:
        try:
            await bot.send_message(admin, text)
        except Exception:
            log.warning("не удалось отправить карточку ученика админу %s", admin)


@router.callback_query(F.data == "adm:csv")
async def export_csv(cb: CallbackQuery, db: Database):
    await cb.answer()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Подписано (UTC)", "Статус", "Сумма", "Курс", "Клиент", "Телефон", "E-mail"])
    names = {"signed": "подписано, не оплачено", "claimed": "ждёт проверки оплаты",
             "paid": "оплачено", "rejected": "оплата не найдена"}
    for r in db.orders_report():
        w.writerow([r["id"], r["signed_at"], names.get(r["status"], r["status"]), r["amount"],
                    r["title"], r["full_name"], r["phone"], r["email"]])
    data = buf.getvalue().encode("utf-8-sig")
    await cb.message.answer_document(BufferedInputFile(data, filename="zakazy.csv"))
