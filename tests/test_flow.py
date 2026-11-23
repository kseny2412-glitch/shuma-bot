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
    db.add_slots(o2.id, ["2099-12-01T15:30:00+00:00"])
    slot = db.free_slots(o2.id)[0]
    signed = sign_draft(db, cfg, client, o2, d2, "console", slot_id=slot["id"])
    assert len(signed.files) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 1


def test_missing_executor_fields_become_blanks(db, cfg, client):
    from dataclasses import replace
    from app.config import Executor
    cfg2 = replace(cfg, executor=Executor())
    offer = db.create_offer("Курс", "course", 1000)
    d = make_draft(db, cfg2, client, offer)
    assert "________" in d.contract_text


def test_contract_numbers_are_sequential_and_start_configurable(db, cfg, client):
    import pytest
    from dataclasses import replace
    from app.services import NumberTaken
    offer = db.create_offer("Курс", "course", 1000)
    d1 = make_draft(db, cfg, client, offer)
    assert d1.contract_no == "1" and "ДОГОВОР № 1" in d1.contract_text
    assert d1.order_no == "1/1"
    sign_draft(db, cfg, client, offer, d1, "console")
    assert db.get_contract(client.tg_id)["number"] == "1"
    assert make_draft(db, cfg, client, offer).order_no == "1/2"   # повторная покупка

    db.set_setting("contract_start", "25")                        # продолжить бумажные
    c2 = replace(client, tg_id=client.tg_id + 1)
    db.save_client(c2)
    c3 = replace(client, tg_id=client.tg_id + 2)
    db.save_client(c3)
    d2, d3 = make_draft(db, cfg, c2, offer), make_draft(db, cfg, c3, offer)
    assert d2.contract_no == d3.contract_no == "25"
    sign_draft(db, cfg, c2, offer, d2, "console")
    with pytest.raises(NumberTaken):                              # одновременная подпись
        sign_draft(db, cfg, c3, offer, d3, "console")
    assert make_draft(db, cfg, c3, offer).contract_no == "26"
    db.set_setting("contract_start", "3")                         # меньше выданных — игнор
    assert db.next_contract_number() == 26
    row = [r for r in db.orders_report() if r["full_name"] == client.full_name][0]
    assert row["contract_no"] in ("1", "25")
