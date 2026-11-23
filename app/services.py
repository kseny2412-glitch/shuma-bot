"""Логика оформления: черновики документов, подписание, номера."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .db import Client, Database, Offer, now_iso
from .documents import SignatureStamp, render_text, text_sha256, write_pdf


def local_now(cfg: Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.timezone))


def fmt_price(rub: int) -> str:
    return f"{rub:,}".replace(",", " ")


def slot_label(cfg: Config, starts_at: str) -> str:
    local = datetime.fromisoformat(starts_at).astimezone(ZoneInfo(cfg.timezone))
    return f"{local:%d.%m.%Y %H:%M} ({cfg.timezone})"


@dataclass
class Draft:
    """То, что клиент видит перед подписью. Подписывается ровно этот текст."""
    contract_no: str
    contract_text: str | None   # None — договор уже подписан раньше
    application_text: str
    order_no: str


def make_draft(db: Database, cfg: Config, client: Client, offer: Offer) -> Draft:
    now = local_now(cfg)
    existing = db.get_contract(client.tg_id)
    if existing:
        contract_no = existing["number"] or Path(existing["pdf_path"]).stem.removeprefix("contract_")
    else:
        contract_no = str(db.next_contract_number())
    n_orders = db.conn.execute("SELECT COUNT(*) FROM orders WHERE tg_id=?",
                               (client.tg_id,)).fetchone()[0]
    order_no = f"{contract_no}/{n_orders + 1}"
    fields = {
        "contract_no": contract_no,
        "order_no": order_no,
        "city": f"г. {cfg.executor.city}" if cfg.executor.city else "",
        "date": f"{now:%d.%m.%Y}",
        "executor_name": cfg.executor.name,
        "executor_inn": cfg.executor.inn,
        "executor_ogrnip": cfg.executor.ogrnip,
        "client_name": client.full_name,
        "client_birth_date": client.birth_date,
        "client_phone": client.phone,
        "client_email": client.email,
        "offer_title": offer.title,
        "offer_kind": offer.kind_label,
        "offer_schedule": offer.schedule,
        "offer_address": offer.address,
        "offer_description": offer.description,
        "offer_price": fmt_price(offer.price),
    }
    contract_text = None if existing else render_text("contract.txt", fields)
    return Draft(contract_no, contract_text, render_text("application.txt", fields), order_no)


def preview_pdfs(cfg: Config, draft: Draft, tg_id: int) -> list[Path]:
    out = cfg.data_dir / "previews"
    files = []
    if draft.contract_text:
        files.append(write_pdf(draft.contract_text, out / f"Договор_{tg_id}.pdf"))
    files.append(write_pdf(draft.application_text, out / f"Заявка_{tg_id}.pdf"))
    return files


class NumberTaken(Exception):
    """Пока клиент читал договор, этот номер получил другой клиент."""


class SlotUnavailable(Exception):
    """Время больше недоступно для записи."""


@dataclass
class Signed:
    order_id: int
    files: list[Path]


def sign_draft(db: Database, cfg: Config, client: Client, offer: Offer, draft: Draft,
               provider: str, *, slot_id: int | None = None) -> Signed:
    """Вызывается только после верного кода. Пишет подписанные PDF и журнал."""
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        current = db.get_offer(offer.id)
        if (not current or not current.is_active or db.seats_left(current) == 0
                or (current.kind == "individual" and not db.available_slot(current.id, slot_id))):
            raise SlotUnavailable
        return _sign_documents(db, cfg, client, offer, draft, provider, slot_id)


def _sign_documents(db: Database, cfg: Config, client: Client, offer: Offer, draft: Draft,
                    provider: str, slot_id: int | None) -> Signed:
    signed_at = now_iso()
    human_time = f"{local_now(cfg):%d.%m.%Y %H:%M:%S %Z}"
    executor = f"ИП {cfg.executor.name}, ИНН {cfg.executor.inn}".strip()
    docs = cfg.docs_dir / str(client.tg_id)
    files: list[Path] = []
    if draft.contract_text:
        if db.get_contract(client.tg_id) or db.contract_number_taken(draft.contract_no):
            raise NumberTaken(draft.contract_no)
        sha = text_sha256(draft.contract_text)
        path = docs / f"contract_{draft.contract_no}.pdf"
        write_pdf(draft.contract_text, path, SignatureStamp(
            client.full_name, client.phone, human_time, sha, secrets.token_hex(8), executor))
        db.add_contract(client.tg_id, client.phone, sha, str(path), signed_at, provider,
                        number=draft.contract_no, commit=False)
        files.append(path)
    sha = text_sha256(draft.application_text)
    path = docs / f"application_{draft.order_no.replace('/', '_')}.pdf"
    write_pdf(draft.application_text, path, SignatureStamp(
        client.full_name, client.phone, human_time, sha, secrets.token_hex(8), executor))
    order_id = db.add_order(client.tg_id, offer, client.phone, sha, str(path), signed_at, provider,
                            slot_id=slot_id, commit=False)
    files.append(path)
    return Signed(order_id, files)
