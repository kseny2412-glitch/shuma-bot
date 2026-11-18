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
        contract_no = Path(existing["pdf_path"]).stem.removeprefix("contract_")
    else:
        contract_no = f"{now:%Y%m%d}-{client.tg_id}"
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


@dataclass
class Signed:
    order_id: int
    files: list[Path]


def sign_draft(db: Database, cfg: Config, client: Client, offer: Offer, draft: Draft,
               provider: str) -> Signed:
    """Вызывается только после верного кода. Пишет подписанные PDF и журнал."""
    signed_at = now_iso()
    human_time = f"{local_now(cfg):%d.%m.%Y %H:%M:%S %Z}"
    executor = f"ИП {cfg.executor.name}, ИНН {cfg.executor.inn}".strip()
    docs = cfg.docs_dir / str(client.tg_id)
    files: list[Path] = []
    if draft.contract_text and not db.get_contract(client.tg_id):
        sha = text_sha256(draft.contract_text)
        path = docs / f"contract_{draft.contract_no}.pdf"
        write_pdf(draft.contract_text, path, SignatureStamp(
            client.full_name, client.phone, human_time, sha, secrets.token_hex(8), executor))
        db.add_contract(client.tg_id, client.phone, sha, str(path), signed_at, provider)
        files.append(path)
    sha = text_sha256(draft.application_text)
    path = docs / f"application_{draft.order_no.replace('/', '_')}.pdf"
    write_pdf(draft.application_text, path, SignatureStamp(
        client.full_name, client.phone, human_time, sha, secrets.token_hex(8), executor))
    order_id = db.add_order(client.tg_id, offer, client.phone, sha, str(path), signed_at, provider)
    files.append(path)
    return Signed(order_id, files)
