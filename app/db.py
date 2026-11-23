"""Хранилище: клиенты, предложения (курсы/занятия), договоры, заявки-заказы, коды подписи.

SQLite, синхронно: нагрузка у бота маленькая.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    tg_id          INTEGER PRIMARY KEY,
    full_name      TEXT NOT NULL,
    birth_date     TEXT NOT NULL,
    phone          TEXT NOT NULL,
    email          TEXT NOT NULL,
    pd_consent_at  TEXT NOT NULL,   -- согласие на обработку ПД
    pep_consent_at TEXT NOT NULL,   -- согласие с правилами ПЭП
    photo_consent  INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- course | individual
    price       INTEGER NOT NULL,   -- рубли
    schedule    TEXT NOT NULL DEFAULT '',
    address     TEXT NOT NULL DEFAULT '',
    seats       INTEGER,            -- NULL = без ограничения
    description TEXT NOT NULL DEFAULT '',
    is_public   INTEGER NOT NULL DEFAULT 1,  -- 0 = только по личной ссылке
    is_active   INTEGER NOT NULL DEFAULT 1,
    offline_taken INTEGER NOT NULL DEFAULT 0,  -- записаны вне бота (занимают места)
    is_archived INTEGER NOT NULL DEFAULT 0,    -- «удалён» админом, но есть договоры
    sort_order  INTEGER NOT NULL DEFAULT 0,    -- порядок в меню, меньше = выше
    link_token  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL REFERENCES clients(tg_id),
    signed_at  TEXT NOT NULL,
    phone      TEXT NOT NULL,
    doc_sha256 TEXT NOT NULL,
    pdf_path   TEXT NOT NULL,
    number     TEXT              -- номер договора (сквозной: 1, 2, 3…)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id       INTEGER NOT NULL REFERENCES clients(tg_id),
    offer_id    INTEGER NOT NULL REFERENCES offers(id),
    amount      INTEGER NOT NULL,
    status      TEXT NOT NULL,      -- signed | claimed | paid | rejected | cancelled
    signed_at   TEXT NOT NULL,
    doc_sha256  TEXT NOT NULL,
    pdf_path    TEXT NOT NULL,
    claimed_at  TEXT,
    decided_at  TEXT,
    decided_by  INTEGER
);

CREATE TABLE IF NOT EXISTS sign_codes (
    tg_id      INTEGER PRIMARY KEY,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lesson_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    starts_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE (offer_id, starts_at)
);

-- Журнал подписаний: доказательство для споров, только добавление.
CREATE TABLE IF NOT EXISTS signature_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL,
    phone      TEXT NOT NULL,
    doc_type   TEXT NOT NULL,       -- contract | application
    doc_id     INTEGER NOT NULL,
    doc_sha256 TEXT NOT NULL,
    signed_at  TEXT NOT NULL,
    sms_provider TEXT NOT NULL
);
"""

ACTIVE_ORDER_STATUSES = ("signed", "claimed", "paid")
FREE_SLOT = """enabled=1 AND NOT EXISTS (SELECT 1 FROM orders
               WHERE orders.slot_id=lesson_slots.id AND status IN ('signed','claimed','paid'))"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Client:
    tg_id: int
    full_name: str
    birth_date: str
    phone: str
    email: str
    photo_consent: bool


@dataclass
class Offer:
    id: int
    title: str
    kind: str
    price: int
    schedule: str
    address: str
    seats: Optional[int]
    description: str
    is_public: bool
    is_active: bool
    link_token: str
    offline_taken: int = 0
    is_archived: bool = False

    @property
    def kind_label(self) -> str:
        return "Групповой курс" if self.kind == "course" else "Индивидуальная тренировка"


@dataclass
class Order:
    id: int
    tg_id: int
    offer_id: int
    amount: int
    status: str
    signed_at: str
    pdf_path: str
    slot_id: int | None = None


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(clients)")}
        if "username" not in cols:  # миграция для баз, созданных до 2026-11-20
            self.conn.execute("ALTER TABLE clients ADD COLUMN username TEXT")
        if "number" not in {r[1] for r in self.conn.execute("PRAGMA table_info(contracts)")}:
            self.conn.execute("ALTER TABLE contracts ADD COLUMN number TEXT")
        if "slot_id" not in {r[1] for r in self.conn.execute("PRAGMA table_info(orders)")}:
            self.conn.execute("ALTER TABLE orders ADD COLUMN slot_id INTEGER REFERENCES lesson_slots(id)")
        self.conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS booked_slot ON orders(slot_id)
                             WHERE status IN ('signed','claimed','paid')""")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(offers)")}
        if "offline_taken" not in cols:
            self.conn.execute(
                "ALTER TABLE offers ADD COLUMN offline_taken INTEGER NOT NULL DEFAULT 0")
        if "is_archived" not in cols:
            self.conn.execute(
                "ALTER TABLE offers ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")
        if "sort_order" not in cols:  # сохраняем прежний порядок: новые сверху
            self.conn.execute(
                "ALTER TABLE offers ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE offers SET sort_order = -id")
        self.conn.commit()

    # --- клиенты ---
    def save_client(self, c: Client) -> None:
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO clients (tg_id, full_name, birth_date, phone, email,
                   pd_consent_at, pep_consent_at, photo_consent, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET full_name=excluded.full_name,
                   birth_date=excluded.birth_date, phone=excluded.phone,
                   email=excluded.email, photo_consent=excluded.photo_consent""",
            (c.tg_id, c.full_name, c.birth_date, c.phone, c.email, ts, ts,
             int(c.photo_consent), ts),
        )
        self.conn.commit()

    def set_username(self, tg_id: int, username: Optional[str]) -> None:
        """Ник в Telegram (без @). Обновляем при каждом нажатии «Я оплатил(а)»."""
        self.conn.execute("UPDATE clients SET username=? WHERE tg_id=?", (username, tg_id))
        self.conn.commit()

    def get_username(self, tg_id: int) -> Optional[str]:
        r = self.conn.execute("SELECT username FROM clients WHERE tg_id=?", (tg_id,)).fetchone()
        return r["username"] if r else None

    def get_client(self, tg_id: int) -> Optional[Client]:
        r = self.conn.execute("SELECT * FROM clients WHERE tg_id=?", (tg_id,)).fetchone()
        if not r:
            return None
        return Client(r["tg_id"], r["full_name"], r["birth_date"], r["phone"],
                      r["email"], bool(r["photo_consent"]))

    # --- предложения ---
    def create_offer(self, title: str, kind: str, price: int, schedule: str = "",
                     address: str = "", seats: Optional[int] = None,
                     description: str = "", is_public: bool = True) -> Offer:
        if kind not in ("course", "individual"):
            raise ValueError("kind must be course or individual")
        if price <= 0:
            raise ValueError("price must be positive")
        cur = self.conn.execute(
            """INSERT INTO offers (title, kind, price, schedule, address, seats,
                   description, is_public, link_token, created_at, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       (SELECT COALESCE(MIN(sort_order), 0) - 1 FROM offers))""",
            (title, kind, price, schedule, address, seats, description,
             int(is_public), secrets.token_urlsafe(8), now_iso()),
        )
        self.conn.commit()
        return self.get_offer(cur.lastrowid)  # type: ignore[return-value]

    @staticmethod
    def _offer(r: sqlite3.Row) -> Offer:
        return Offer(r["id"], r["title"], r["kind"], r["price"], r["schedule"],
                     r["address"], r["seats"], r["description"], bool(r["is_public"]),
                     bool(r["is_active"]), r["link_token"], r["offline_taken"],
                     bool(r["is_archived"]))

    def get_offer(self, offer_id: int) -> Optional[Offer]:
        r = self.conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        return self._offer(r) if r else None

    def get_offer_by_token(self, token: str) -> Optional[Offer]:
        r = self.conn.execute("SELECT * FROM offers WHERE link_token=?", (token,)).fetchone()
        return self._offer(r) if r else None

    def list_offers(self, public_only: bool = False) -> list[Offer]:
        q = "SELECT * FROM offers WHERE is_archived=0"
        if public_only:
            q += " AND is_public=1 AND is_active=1"
        q += " ORDER BY sort_order, id DESC"
        return [self._offer(r) for r in self.conn.execute(q)]

    def remove_offer(self, offer_id: int) -> str:
        """Удаляет предложение. Если по нему есть заявки (подписанные документы,
        оплаты), не стирает, а убирает в архив: из списков пропадает, данные остаются.
        Возвращает "deleted" или "archived"."""
        n = self.conn.execute("SELECT COUNT(*) FROM orders WHERE offer_id=?",
                              (offer_id,)).fetchone()[0]
        if n:
            self.conn.execute("UPDATE offers SET is_archived=1, is_active=0 WHERE id=?",
                              (offer_id,))
            result = "archived"
        else:
            self.conn.execute("DELETE FROM offers WHERE id=?", (offer_id,))
            result = "deleted"
        self.conn.commit()
        return result

    def move_offer(self, offer_id: int, direction: int) -> tuple[int, int]:
        """Сдвигает предложение на одну позицию (-1 вверх, +1 вниз) среди неархивных.
        Возвращает (новая позиция с 1, всего)."""
        ids = [o.id for o in self.list_offers()]
        i = ids.index(offer_id)
        j = i + direction
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            self.conn.executemany("UPDATE offers SET sort_order=? WHERE id=?",
                                  [(n, oid) for n, oid in enumerate(ids)])
            self.conn.commit()
            i = j
        return i + 1, len(ids)

    def set_offer_active(self, offer_id: int, active: bool) -> None:
        self.conn.execute("UPDATE offers SET is_active=? WHERE id=?", (int(active), offer_id))
        self.conn.commit()

    def set_offer_seats(self, offer_id: int, seats: Optional[int]) -> None:
        if seats is not None and seats < 0:
            raise ValueError("seats must be >= 0")
        self.conn.execute("UPDATE offers SET seats=? WHERE id=?", (seats, offer_id))
        self.conn.commit()

    def set_offline_taken(self, offer_id: int, n: int) -> None:
        if n < 0:
            raise ValueError("n must be >= 0")
        self.conn.execute("UPDATE offers SET offline_taken=? WHERE id=?", (n, offer_id))
        self.conn.commit()

    def offer_students(self, offer_id: int) -> list[sqlite3.Row]:
        """Действующие записи на предложение (занимают место)."""
        return list(self.conn.execute(
            f"""SELECT o.id, o.status, o.amount, o.slot_id, c.full_name, c.phone, c.username
               FROM orders o JOIN clients c ON c.tg_id=o.tg_id
               WHERE o.offer_id=? AND o.status IN ({','.join('?' * len(ACTIVE_ORDER_STATUSES))})
               ORDER BY o.id""", (offer_id, *ACTIVE_ORDER_STATUSES)))

    def seats_left(self, offer: Offer) -> Optional[int]:
        if offer.seats is None:
            return None
        taken = self.conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE offer_id=? AND status IN "
            f"({','.join('?' * len(ACTIVE_ORDER_STATUSES))})",
            (offer.id, *ACTIVE_ORDER_STATUSES),
        ).fetchone()[0]
        return max(offer.seats - taken - offer.offline_taken, 0)

    # --- договоры ---
    def get_contract(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM contracts WHERE tg_id=? ORDER BY id DESC LIMIT 1", (tg_id,)
        ).fetchone()

    def add_contract(self, tg_id: int, phone: str, sha: str, pdf_path: str,
                     signed_at: str, sms_provider: str, number: Optional[str] = None,
                     *, commit: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO contracts (tg_id, signed_at, phone, doc_sha256, pdf_path, number) "
            "VALUES (?,?,?,?,?,?)",
            (tg_id, signed_at, phone, sha, pdf_path, number),
        )
        self._log(tg_id, phone, "contract", cur.lastrowid, sha, signed_at, sms_provider)
        if commit:
            self.conn.commit()
        return cur.lastrowid

    def next_contract_id(self) -> int:
        r = self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM contracts").fetchone()
        return r[0]

    # --- сквозная нумерация договоров ---
    def get_setting(self, key: str, default: str = "") -> str:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.conn.commit()

    def next_contract_number(self) -> int:
        """Следующий свободный номер: больше всех выданных и не меньше заданного админом."""
        used = self.conn.execute(
            "SELECT COALESCE(MAX(CAST(number AS INTEGER)), 0) FROM contracts "
            "WHERE number GLOB '[0-9]*'").fetchone()[0]
        return max(used + 1, int(self.get_setting("contract_start", "1")))

    def contract_number_taken(self, number: str) -> bool:
        return self.conn.execute("SELECT 1 FROM contracts WHERE number=?",
                                 (number,)).fetchone() is not None

    # --- заказы (заявки) ---
    def add_slots(self, offer_id: int, starts: list[str]) -> int:
        with self.conn:
            cur = self.conn.executemany(
                """INSERT INTO lesson_slots (offer_id, starts_at) VALUES (?,?)
                   ON CONFLICT(offer_id, starts_at) DO UPDATE SET enabled=1 WHERE enabled=0""",
                [(offer_id, start) for start in starts],
            )
        return cur.rowcount

    def free_slots(self, offer_id: int, offset: int = 0, limit: int = 16) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            f"""SELECT * FROM lesson_slots WHERE offer_id=? AND starts_at>? AND {FREE_SLOT}
                ORDER BY starts_at LIMIT ? OFFSET ?""", (offer_id, now_iso(), limit, offset)))

    def available_slot(self, offer_id: int, slot_id: int | None) -> sqlite3.Row | None:
        return self.conn.execute(
            f"""SELECT * FROM lesson_slots
                WHERE id=? AND offer_id=? AND starts_at>? AND {FREE_SLOT}""",
            (slot_id, offer_id, now_iso())).fetchone()

    def remove_slot(self, offer_id: int, slot_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE lesson_slots SET enabled=0 WHERE id=? AND offer_id=? AND {FREE_SLOT}",
                (slot_id, offer_id))
        return cur.rowcount == 1

    def slot_time(self, slot_id: int | None) -> str | None:
        row = self.conn.execute("SELECT starts_at FROM lesson_slots WHERE id=?", (slot_id,)).fetchone()
        return row[0] if row else None

    def add_order(self, tg_id: int, offer: Offer, phone: str, sha: str, pdf_path: str,
                  signed_at: str, sms_provider: str, *, slot_id: int | None = None,
                  commit: bool = True) -> int:
        cur = self.conn.execute(
            """INSERT INTO orders (tg_id, offer_id, amount, status, signed_at, doc_sha256, pdf_path, slot_id)
               VALUES (?, ?, ?, 'signed', ?, ?, ?, ?)""",
            (tg_id, offer.id, offer.price, signed_at, sha, pdf_path, slot_id),
        )
        self._log(tg_id, phone, "application", cur.lastrowid, sha, signed_at, sms_provider)
        if commit:
            self.conn.commit()
        return cur.lastrowid

    def next_order_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM orders").fetchone()[0]

    def get_order(self, order_id: int) -> Optional[Order]:
        r = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not r:
            return None
        return Order(r["id"], r["tg_id"], r["offer_id"], r["amount"], r["status"],
                     r["signed_at"], r["pdf_path"], r["slot_id"])

    def set_order_status(self, order_id: int, status: str, by: Optional[int] = None) -> bool:
        """Меняет статус по правилам. Возвращает False, если переход недопустим."""
        allowed = {"claimed": ("signed",), "paid": ("signed", "claimed"),
                   "rejected": ("signed", "claimed"),
                   "cancelled": ACTIVE_ORDER_STATUSES}  # отмена записи админом
        order = self.get_order(order_id)
        if not order or order.status not in allowed.get(status, ()):
            return False
        col = "claimed_at" if status == "claimed" else "decided_at"
        self.conn.execute(
            f"UPDATE orders SET status=?, {col}=?, decided_by=COALESCE(?, decided_by) WHERE id=?",
            (status, now_iso(), by, order_id),
        )
        self.conn.commit()
        return True

    def orders_report(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT o.id, o.signed_at, o.status, o.amount, f.title, c.full_name, c.phone, c.email,
                      (SELECT k.number FROM contracts k WHERE k.tg_id=o.tg_id
                       ORDER BY k.id DESC LIMIT 1) AS contract_no
               FROM orders o JOIN offers f ON f.id=o.offer_id JOIN clients c ON c.tg_id=o.tg_id
               ORDER BY o.id DESC"""))

    def pending_orders(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT o.id, o.amount, o.status, o.slot_id, f.title, c.full_name, c.phone
               FROM orders o JOIN offers f ON f.id=o.offer_id JOIN clients c ON c.tg_id=o.tg_id
               WHERE o.status='claimed' ORDER BY o.id"""))

    # --- коды подписи ---
    def put_code(self, tg_id: int, code_hash: str, expires_at: str) -> None:
        self.conn.execute(
            """INSERT INTO sign_codes (tg_id, code_hash, expires_at, attempts) VALUES (?,?,?,0)
               ON CONFLICT(tg_id) DO UPDATE SET code_hash=excluded.code_hash,
                   expires_at=excluded.expires_at, attempts=0""",
            (tg_id, code_hash, expires_at),
        )
        self.conn.commit()

    def get_code(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM sign_codes WHERE tg_id=?", (tg_id,)).fetchone()

    def bump_attempts(self, tg_id: int) -> None:
        self.conn.execute("UPDATE sign_codes SET attempts=attempts+1 WHERE tg_id=?", (tg_id,))
        self.conn.commit()

    def drop_code(self, tg_id: int) -> None:
        self.conn.execute("DELETE FROM sign_codes WHERE tg_id=?", (tg_id,))
        self.conn.commit()

    def _log(self, tg_id, phone, doc_type, doc_id, sha, signed_at, provider) -> None:
        self.conn.execute(
            """INSERT INTO signature_log (tg_id, phone, doc_type, doc_id, doc_sha256, signed_at, sms_provider)
               VALUES (?,?,?,?,?,?,?)""",
            (tg_id, phone, doc_type, doc_id, sha, signed_at, provider),
        )
