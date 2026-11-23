"""Фоновая выгрузка подписанных документов и контактов на Яндекс Диск."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
from pathlib import Path, PurePosixPath

import aiohttp

from .config import Config
from .db import Database

log = logging.getLogger(__name__)
API = "https://cloud-api.yandex.net/v1/disk/resources"


class DiskError(Exception):
    pass


def clients_csv(db: Database) -> bytes:
    def cell(value: str | None) -> str:
        value = value or ""
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
        return value

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["ФИО", "Телефон", "Email", "Telegram", "Telegram ID"])
    for row in db.conn.execute(
        "SELECT full_name, phone, email, username, tg_id FROM clients ORDER BY full_name, tg_id"
    ):
        writer.writerow([cell(value) for value in tuple(row)[:4]] + [row["tg_id"]])
    return stream.getvalue().encode("utf-8-sig")


class DiskSync:
    def __init__(self, cfg: Config, db: Database):
        raw_path = cfg.disk_path.removeprefix("disk:")
        self.folder = PurePosixPath(raw_path)
        if not raw_path.startswith("/") or self.folder == PurePosixPath("/") or ".." in self.folder.parts:
            raise ValueError("YANDEX_DISK_PATH должен указывать на папку, например /Шума/Документы")
        self.cfg = cfg
        self.db = db
        self.headers = {"Authorization": f"OAuth {cfg.disk_token}"}
        self.uploaded_documents: set[tuple[str, int, str]] = set()
        self.uploaded_contacts: bytes | None = None

    async def get(self, session: aiohttp.ClientSession, suffix: str, **params):
        async with session.get(API + suffix, params=params, headers=self.headers,
                               allow_redirects=False) as response:
            if response.status != 200:
                raise DiskError(f"Диск HTTP {response.status}")
            return await response.json()

    async def verify_folder(self, session: aiohttp.ClientSession) -> None:
        for folder in (self.folder, *self.folder.parents):
            meta = await self.get(session, "", path=f"disk:{folder}",
                                  fields="type,public_key,public_url")
            if meta.get("type") != "dir" or meta.get("public_key") or meta.get("public_url"):
                raise DiskError("Папка или её родитель опубликованы либо указана не папка")

    async def upload(self, session: aiohttp.ClientSession, name: str, content: bytes) -> None:
        target = await self.get(session, "/upload", path=f"disk:{self.folder / name}",
                                overwrite="true")
        href = target.get("href", "")
        if not href.startswith("https://"):
            raise DiskError("Диск вернул неверную ссылку загрузки")
        async with session.put(href, data=content, allow_redirects=False) as response:
            if response.status not in (200, 201, 202, 204):
                raise DiskError(f"Загрузка HTTP {response.status}")

    async def sync_once(self, session: aiohttp.ClientSession) -> None:
        await self.verify_folder(session)
        contacts = clients_csv(self.db)
        digest = hashlib.sha256(contacts).digest()
        if digest != self.uploaded_contacts:
            await self.upload(session, "Клиенты.csv", contacts)
            self.uploaded_contacts = digest

        rows = list(self.db.conn.execute(
            """SELECT 'Договор' AS kind, id, pdf_path FROM contracts
               UNION ALL SELECT 'Заявка', id, pdf_path FROM orders"""
        ))
        for kind, document_id, filename in rows:
            key = (kind, document_id, filename)
            if key in self.uploaded_documents:
                continue
            path = Path(filename).resolve()
            if not path.is_relative_to(self.cfg.docs_dir.resolve()) or path.suffix.lower() != ".pdf":
                log.error("Некорректный путь к подписанному PDF, запись %s %s", kind, document_id)
                continue
            try:
                content = await asyncio.to_thread(path.read_bytes)
                await self.upload(session, f"{kind}_{document_id}.pdf", content)
            except (OSError, aiohttp.ClientError, TimeoutError, DiskError) as error:
                log.warning("Не удалось выгрузить %s %s: %s", kind, document_id, type(error).__name__)
                continue
            self.uploaded_documents.add(key)

    async def run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            while True:
                try:
                    await self.sync_once(session)
                except DiskError as error:
                    log.warning("Выгрузка на Яндекс Диск приостановлена: %s", error)
                except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                    log.warning("Ошибка выгрузки на Яндекс Диск: %s", type(error).__name__)
                await asyncio.sleep(60)
