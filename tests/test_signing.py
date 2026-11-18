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
