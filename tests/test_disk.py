from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.disk import DiskError, DiskSync
from app.services import make_draft, sign_draft


async def test_sync_existing_documents_and_retry(db, cfg, client):
    cfg = replace(cfg, disk_token="test", disk_path="/Шума/Документы")
    offer = db.create_offer("Курс", "course", 1000)
    signed = sign_draft(db, cfg, client, offer, make_draft(db, cfg, client, offer), "console")
    sync = DiskSync(cfg, db)
    sync.verify_folder = AsyncMock()
    sync.upload = AsyncMock(side_effect=[None, DiskError("временно"), None, None])

    await sync.sync_once(None)
    await sync.sync_once(None)
    await sync.sync_once(None)

    calls = [call.args[1:] for call in sync.upload.await_args_list]
    assert [name for name, _ in calls] == [
        "Клиенты.csv", "Договор_1.pdf", "Заявка_1.pdf", "Договор_1.pdf"
    ]
    assert client.phone in calls[0][1].decode("utf-8-sig")
    assert calls[-1][1] == signed.files[0].read_bytes()


async def test_published_parent_blocks_upload(db, cfg):
    sync = DiskSync(replace(cfg, disk_token="test", disk_path="/Шума/Документы"), db)
    sync.get = AsyncMock(side_effect=[
        {"type": "dir"}, {"type": "dir", "public_url": "https://disk.yandex.ru/d/example"}
    ])
    sync.upload = AsyncMock()

    with pytest.raises(DiskError):
        await sync.sync_once(None)
    sync.upload.assert_not_awaited()
