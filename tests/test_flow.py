from app.documents import text_sha256
from app.services import make_draft, preview_pdfs, sign_draft


def test_first_purchase_signs_contract_and_application(db, cfg, client):
    offer = db.create_offer("Интенсив «Бачата»", "course", 12000, "1–5 декабря", "ул. Танцевальная, 1")
    draft = make_draft(db, cfg, client, offer)
    assert client.full_name in draft.contract_text
    assert "Иванова Анна Сергеевна" in draft.contract_text
    assert "12 000" in draft.application_text
    assert "{" not in draft.contract_text and "{" not in draft.application_text
    assert all(p.stat().st_size > 1000 for p in preview_pdfs(cfg, draft, client.tg_id))

    signed = sign_draft(db, cfg, client, offer, draft, "console")
    assert len(signed.files) == 2 and all(p.exists() for p in signed.files)
    contract = db.get_contract(client.tg_id)
    assert contract["doc_sha256"] == text_sha256(draft.contract_text)
    log = db.conn.execute("SELECT doc_type FROM signature_log ORDER BY id").fetchall()
    assert [r[0] for r in log] == ["contract", "application"]


def test_second_purchase_only_application(db, cfg, client):
    o1 = db.create_offer("Курс 1", "course", 10000)
    sign_draft(db, cfg, client, o1, make_draft(db, cfg, client, o1), "console")
    o2 = db.create_offer("Индивидуалка", "individual", 4500, is_public=False)
    d2 = make_draft(db, cfg, client, o2)
    assert d2.contract_text is None
    assert d2.order_no.endswith("/2")
    assert d2.contract_no in d2.application_text
    signed = sign_draft(db, cfg, client, o2, d2, "console")
    assert len(signed.files) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 1


def test_missing_executor_fields_become_blanks(db, cfg, client):
    from dataclasses import replace
    from app.config import Executor
    cfg2 = replace(cfg, executor=Executor())
    offer = db.create_offer("Курс", "course", 1000)
    d = make_draft(db, cfg2, client, offer)
    assert "________" in d.contract_text
