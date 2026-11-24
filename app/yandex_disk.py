"""Загрузка файлов на Яндекс.Диск."""
from __future__ import annotations

import logging
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

API = "https://cloud-api.yandex.net/v1/disk"


async def upload_file(token: str, local_path: Path, remote_path: str) -> bool:
    """Загружает файл на Яндекс.Диск. Возвращает True при успехе."""
    if not token:
        return False
    headers = {"Authorization": f"OAuth {token}"}
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем URL для загрузки
            async with session.get(
                f"{API}/resources/upload",
                params={"path": remote_path, "overwrite": "true"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    log.warning("Yandex Disk upload URL failed: %s", await resp.text())
                    return False
                data = await resp.json()
                href = data.get("href")
                if not href:
                    return False

            # Загружаем файл
            with local_path.open("rb") as f:
                async with session.put(href, data=f, timeout=aiohttp.ClientTimeout(total=120)) as put:
                    if put.status in (201, 202):
                        log.info("Uploaded to Yandex Disk: %s", remote_path)
                        return True
                    log.warning("Yandex Disk upload failed: %s", put.status)
                    return False
    except Exception:
        log.exception("Yandex Disk upload error")
        return False
