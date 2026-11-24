"""Сторона клиента: регистрация, выбор курса, подпись по SMS-коду, ссылка на оплату."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove)

from ..config import ROOT, Config
from ..db import Client, Database, Offer
from ..yandex_disk import upload_file
from ..services import NumberTaken, Draft, fmt_price, make_draft, preview_pdfs, sign_draft
from ..signing import check_code, issue_code
from ..sms import SmsError, SmsSender

router = Router(name="client")
log = logging.getLogger(__name__)

WELCOME_PHOTO = ROOT / "assets" / "welcome.jpg"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


class Reg(StatesGroup):
    name = State()
    birth = State()
    phone = State()
    email = State()
    consent = State()
    photo = State()


class Buy(StatesGroup):
    review = State()
    code = State()


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def offer_card(db: Database, o: Offer) -> str:
    lines = [f"<b>{o.title}</b>", o.kind_label, f"Стоимость: <b>{fmt_price(o.price)} ₽</b>"]
    if o.schedule:
        lines.append(f"Когда: {o.schedule}")
    if o.address:
        lines.append(f"Где: {o.address}")
    left = db.seats_left(o)
    if left is not None:
        lines.append(f"Мест в группе: {o.seats}, свободно: {left}")
    if o.description:
        lines.append("")
        lines.append(o.description)
    return "\n".join(lines)


# ---------- старт и меню ----------

@router.message(CommandStart())
async def start(msg: Message, command: CommandObject, state: FSMContext, db: Database):
    await state.clear()
    arg = command.args or ""
    if arg.startswith("o_"):
        offer = db.get_offer_by_token(arg[2:])
        if offer and offer.is_active:
            await state.update_data(offer_id=offer.id)
            return await open_offer(msg, state, db, offer)
        await msg.answer("Это предложение больше не действует. Посмотрите актуальные курсы ниже.")
    if WELCOME_PHOTO.exists():
        await msg.answer_photo(FSInputFile(WELCOME_PHOTO),
                               caption="Привет! Это бот Шумы: здесь можно записаться на курсы "
                                       "и индивидуальные занятия, подписать договор и оплатить.")
    await show_menu(msg, db)


async def show_menu(msg: Message, db: Database):
    offers = db.list_offers(public_only=True)
    if not offers:
        return await msg.answer("Сейчас открытых курсов нет. Загляните позже!")
    rows = [[(o.title, f"offer:{o.id}")] for o in offers]
    await msg.answer("Выберите курс или занятие:", reply_markup=kb(*rows))


@router.callback_query(F.data.startswith("offer:"))
async def offer_chosen(cb: CallbackQuery, state: FSMContext, db: Database):
    offer = db.get_offer(int(cb.data.split(":")[1]))
    await cb.answer()
    if not offer or not offer.is_active:
        return await cb.message.answer("Это предложение больше не действует.")
    await state.update_data(offer_id=offer.id)
    await open_offer(cb.message, state, db, offer, user_id=cb.from_user.id)


async def open_offer(msg: Message, state: FSMContext, db: Database, offer: Offer,
                     user_id: int | None = None):
    left = db.seats_left(offer)
    if left == 0:
        return await msg.answer(offer_card(db, offer) + "\n\nК сожалению, мест больше нет.")
    await msg.answer(offer_card(db, offer),
                     reply_markup=kb([("Записаться", f"buy:{offer.id}")]))


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, state: FSMContext, db: Database, cfg: Config):
    await cb.answer()
    await state.update_data(offer_id=int(cb.data.split(":")[1]))
    client = db.get_client(cb.from_user.id)
    if not client:
        await state.set_state(Reg.name)
        return await cb.message.answer(
            "Для договора нужно несколько данных. Это один раз.\n\n"
            "Напишите фамилию, имя и отчество полностью:")
    await send_draft(cb.message, state, db, cfg, client)


# ---------- регистрация ----------

@router.message(Reg.name, F.text)
async def reg_name(msg: Message, state: FSMContext):
    name = " ".join(msg.text.split())
    if len(name.split()) < 2:
        return await msg.answer("Напишите, пожалуйста, фамилию и имя (и отчество, если есть).")
    await state.update_data(full_name=name)
    await state.set_state(Reg.birth)
    await msg.answer("Дата рождения в формате ДД.ММ.ГГГГ:")


@router.message(Reg.birth, F.text)
async def reg_birth(msg: Message, state: FSMContext):
    if not DATE_RE.match(msg.text.strip()):
        return await msg.answer("Нужен формат ДД.ММ.ГГГГ, например 05.03.1995.")
    await state.update_data(birth_date=msg.text.strip())
    await state.set_state(Reg.phone)
    await msg.answer(
        "Поделитесь номером телефона кнопкой ниже. На него придёт SMS с кодом для подписи.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True))


@router.message(Reg.phone, F.contact)
async def reg_phone(msg: Message, state: FSMContext):
    if msg.contact.user_id != msg.from_user.id:
        return await msg.answer("Нужен ваш собственный номер — нажмите кнопку «Отправить номер».")
    phone = "+" + msg.contact.phone_number.lstrip("+")
    await state.update_data(phone=phone)
    await state.set_state(Reg.email)
    await msg.answer("Ваш e-mail (туда тоже можно присылать документы):",
                     reply_markup=ReplyKeyboardRemove())


@router.message(Reg.phone)
async def reg_phone_text(msg: Message):
    await msg.answer("Пожалуйста, нажмите кнопку «📱 Отправить номер» внизу экрана.")


@router.message(Reg.email, F.text)
async def reg_email(msg: Message, state: FSMContext):
    email = msg.text.strip()
    if not EMAIL_RE.match(email):
        return await msg.answer("Похоже, в адресе ошибка. Попробуйте ещё раз.")
    await state.update_data(email=email)
    await state.set_state(Reg.consent)
    await msg.answer(
        "Последний шаг. Нажимая «Принимаю», вы:\n"
        "• даёте согласие на обработку персональных данных (ФИО, дата рождения, телефон, e-mail) "
        "для заключения и исполнения договора;\n"
        "• соглашаетесь подписывать договор и заявки простой электронной подписью — "
        "одноразовым кодом из SMS, который вы вводите в этом боте. Такой документ равнозначен "
        "подписанному от руки. Код никому не сообщайте.",
        reply_markup=kb([("Принимаю", "consent:yes")], [("Не принимаю", "consent:no")]))


@router.callback_query(Reg.consent, F.data.startswith("consent:"))
async def reg_consent(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data.endswith("no"):
        await state.clear()
        return await cb.message.answer(
            "Без согласия мы не сможем оформить договор в боте. "
            "Если передумаете — нажмите /start.")
    await state.set_state(Reg.photo)
    await cb.message.answer(
        "Можно ли использовать фото и видео с занятий, где вы есть, в соцсетях и рекламе Шумы? "
        "Это необязательно и на запись не влияет.",
        reply_markup=kb([("Да, можно", "photo:yes"), ("Нет", "photo:no")]))


@router.callback_query(Reg.photo, F.data.startswith("photo:"))
async def reg_photo(cb: CallbackQuery, state: FSMContext, db: Database, cfg: Config):
    await cb.answer()
    d = await state.get_data()
    client = Client(cb.from_user.id, d["full_name"], d["birth_date"], d["phone"], d["email"],
                    cb.data.endswith("yes"))
    db.save_client(client)
    await cb.message.answer("Спасибо, данные сохранены.")
    await send_draft(cb.message, state, db, cfg, client)


# ---------- документы и подпись ----------

async def send_draft(msg: Message, state: FSMContext, db: Database, cfg: Config, client: Client):
    d = await state.get_data()
    offer = db.get_offer(d.get("offer_id", 0))
    if not offer or not offer.is_active:
        await state.clear()
        return await msg.answer("Это предложение больше не действует. /start")
    if db.seats_left(offer) == 0:
        await state.clear()
        return await msg.answer("К сожалению, места закончились.")
    draft = make_draft(db, cfg, client, offer)
    await state.update_data(draft=asdict(draft))
    await state.set_state(Buy.review)
    for f in preview_pdfs(cfg, draft, client.tg_id):
        await msg.answer_document(FSInputFile(f))
    what = "договор и заявку" if draft.contract_text else "заявку на курс"
    await msg.answer(
        f"Прочитайте {what}. Если всё верно, нажмите «Подписать» — "
        f"на номер {client.phone} придёт SMS с кодом.",
        reply_markup=kb([("✍️ Подписать", "sign")], [("Отмена", "cancel")]))


@router.callback_query(F.data == "cancel")
async def cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.answer("Отменено. Чтобы начать заново — /start")


@router.callback_query(Buy.review, F.data == "sign")
async def ask_code(cb: CallbackQuery, state: FSMContext, db: Database, sms: SmsSender):
    await cb.answer()
    client = db.get_client(cb.from_user.id)
    code = issue_code(db, client.tg_id)
    try:
        await sms.send(client.phone, f"Код для подписи договора: {code}. Никому его не сообщайте.")
    except SmsError:
        log.exception("SMS не отправлено")
        return await cb.message.answer("Не получилось отправить SMS. Попробуйте через минуту.",
                                       reply_markup=kb([("Отправить ещё раз", "sign")]))
    await state.set_state(Buy.code)
    text = f"Отправили SMS на {client.phone}. Введите код из сообщения:"
    if sms.shows_code_in_chat:
        text += f"\n\n<i>Тестовый режим, SMS не отправляется. Код: {code}</i>"
    await cb.message.answer(text)


@router.callback_query(Buy.code, F.data == "sign")
async def resend_code(cb: CallbackQuery, state: FSMContext, db: Database, sms: SmsSender):
    await state.set_state(Buy.review)
    await ask_code(cb, state, db, sms)


@router.message(Buy.code, F.text)
async def enter_code(msg: Message, state: FSMContext, db: Database, cfg: Config,
                     sms: SmsSender, bot: Bot):
    client = db.get_client(msg.from_user.id)
    res = check_code(db, client.tg_id, msg.text)
    if not res.ok:
        if res.reason == "wrong":
            return await msg.answer("Код неверный. Попробуйте ещё раз.")
        await state.set_state(Buy.review)
        return await msg.answer("Код устарел или было слишком много попыток.",
                                reply_markup=kb([("Получить новый код", "sign")]))
    d = await state.get_data()
    offer = db.get_offer(d["offer_id"])
    draft = Draft(**d["draft"])
    try:
        signed = sign_draft(db, cfg, client, offer, draft, sms.name)
    except NumberTaken:
        # редкий случай: два клиента подписывали одновременно — даём новый номер
        draft = make_draft(db, cfg, client, offer)
        await state.update_data(draft=asdict(draft))
        await state.set_state(Buy.review)
        for f in preview_pdfs(cfg, draft, client.tg_id):
            await msg.answer_document(FSInputFile(f))
        return await msg.answer(
            f"Пока вы читали, номер договора обновился, теперь № {draft.contract_no}. "
            f"Проверьте документы и подпишите ещё раз.",
            reply_markup=kb([("✍️ Подписать", "sign")], [("Отмена", "cancel")]))
    await state.clear()
    await msg.answer("✅ Подписано! Ваши экземпляры документов:")
    for f in signed.files:
        await msg.answer_document(FSInputFile(f))
    # Загрузка договора на Яндекс.Диск
    if cfg.yandex_disk_token and signed.files:
        contract = next((f for f in signed.files if "contract_" in f.name), None)
        if contract:
            # Формат: номер_ФИО.pdf
            safe_name = client.full_name.replace(" ", "_")
            remote = f"disk:/Договоры/{draft.contract_no}_{safe_name}.pdf"
            ok = await upload_file(cfg.yandex_disk_token, contract, remote)
            if ok:
                await msg.answer("Договор загружен на Яндекс.Диск.")
            else:
                log.warning("Не удалось загрузить договор на Яндекс.Диск")
    await msg.answer(
        f"Осталось оплатить <b>{fmt_price(offer.price)} ₽</b>.\n\n"
        f"1. Откройте страницу оплаты: {cfg.payment_url}\n"
        f"2. В поле «Сумма заказа» введите <b>{offer.price}</b>.\n"
        f"3. После оплаты вернитесь сюда и нажмите «Я оплатил(а)».",
        reply_markup=kb([("Я оплатил(а)", f"paid:{signed.order_id}")]),
        disable_web_page_preview=True)
    for admin in cfg.admin_ids:
        try:
            await bot.send_message(
                admin, f"✍️ {client.full_name} подписал(а) заявку «{offer.title}», "
                       f"{fmt_price(offer.price)} ₽. Ждём оплату.\n"
                       f"Телефон: {client.phone}, e-mail: {client.email}")
            for f in signed.files:
                await bot.send_document(admin, FSInputFile(f))
        except Exception:
            log.warning("не удалось уведомить админа %s", admin)


@router.callback_query(F.data.startswith("paid:"))
async def claim_paid(cb: CallbackQuery, db: Database, cfg: Config, bot: Bot):
    order = db.get_order(int(cb.data.split(":")[1]))
    if not order or order.tg_id != cb.from_user.id:
        return await cb.answer()
    if not db.set_order_status(order.id, "claimed"):
        return await cb.answer("Уже отмечено", show_alert=False)
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Спасибо! Шума проверит поступление и подтвердит запись. "
                            "Мы напишем вам здесь.")
    db.set_username(order.tg_id, cb.from_user.username)
    client = db.get_client(order.tg_id)
    offer = db.get_offer(order.offer_id)
    for admin in cfg.admin_ids:
        try:
            await bot.send_message(
                admin,
                f"💳 {client.full_name} ({client.phone}) сообщает об оплате "
                f"«{offer.title}» — {fmt_price(order.amount)} ₽.\nПроверьте поступление.",
                reply_markup=kb([("✅ Оплата получена", f"adm_ok:{order.id}"),
                                 ("❌ Не пришла", f"adm_no:{order.id}")]))
        except Exception:
            log.warning("не удалось уведомить админа %s", admin)
