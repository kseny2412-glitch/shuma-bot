from datetime import datetime, timedelta, timezone

from app.signing import MAX_ATTEMPTS, check_code, issue_code


def test_code_ok_once(db, client):
    code = issue_code(db, client.tg_id)
    assert len(code) == 6
    assert check_code(db, client.tg_id, code).ok
    assert check_code(db, client.tg_id, code).reason == "missing"


def test_wrong_then_locked(db, client):
    code = issue_code(db, client.tg_id)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_ATTEMPTS):
        assert check_code(db, client.tg_id, wrong).reason == "wrong"
    assert check_code(db, client.tg_id, code).reason == "too_many"


def test_expired(db, client):
    code = issue_code(db, client.tg_id)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.conn.execute("UPDATE sign_codes SET expires_at=?", (past,))
    assert check_code(db, client.tg_id, code).reason == "expired"


def test_code_not_stored_in_plain(db, client):
    code = issue_code(db, client.tg_id)
    assert code not in db.get_code(client.tg_id)["code_hash"]


async def test_smsru_sends_sender_name(monkeypatch):
    import app.sms as sms_mod
    sent = {}

    class Resp:
        async def json(self, content_type=None):
            return {"status": "OK", "sms": {"79990000000": {"status": "OK"}}}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Sess:
        def post(self, url, data, timeout):
            sent.update(data); return Resp()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(sms_mod.aiohttp, "ClientSession", Sess)
    await sms_mod.make_sender("smsru", "key", "ShumaDance").send("+79990000000", "x")
    assert sent["from"] == "ShumaDance" and sent["to"] == "79990000000"
    sent.clear()
    await sms_mod.make_sender("smsru", "key").send("+79990000000", "x")
    assert "from" not in sent


async def test_smsru_network_error_is_sms_error(monkeypatch):
    import pytest
    import app.sms as sms_mod

    class Sess:
        async def __aenter__(self): raise sms_mod.aiohttp.ClientConnectionError("down")
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(sms_mod.aiohttp, "ClientSession", Sess)
    with pytest.raises(sms_mod.SmsError):
        await sms_mod.make_sender("smsru", "key").send("+7999", "x")
