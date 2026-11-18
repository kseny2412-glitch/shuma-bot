import pytest


def test_offer_validation(db):
    with pytest.raises(ValueError):
        db.create_offer("x", "course", 0)
    with pytest.raises(ValueError):
        db.create_offer("x", "group", 100)


def test_public_and_private_offers(db):
    pub = db.create_offer("Интенсив", "course", 12000, seats=2)
    priv = db.create_offer("Индивидуалка", "individual", 5000, is_public=False)
    assert [o.id for o in db.list_offers(public_only=True)] == [pub.id]
    assert db.get_offer_by_token(priv.link_token).id == priv.id
    db.set_offer_active(pub.id, False)
    assert db.list_offers(public_only=True) == []


def test_seats_and_status_transitions(db, client):
    o = db.create_offer("Интенсив", "course", 12000, seats=1)
    assert db.seats_left(o) == 1
    oid = db.add_order(client.tg_id, o, client.phone, "h", "p.pdf", "t", "console")
    assert db.seats_left(o) == 0
    assert not db.set_order_status(oid, "signed")
    assert db.set_order_status(oid, "claimed")
    assert db.set_order_status(oid, "paid", by=1)
    assert not db.set_order_status(oid, "rejected")  # второй админ не может перерешить
    assert db.get_order(oid).status == "paid"


def test_rejected_frees_seat(db, client):
    o = db.create_offer("Интенсив", "course", 12000, seats=1)
    oid = db.add_order(client.tg_id, o, client.phone, "h", "p.pdf", "t", "console")
    db.set_order_status(oid, "rejected", by=1)
    assert db.seats_left(o) == 1
