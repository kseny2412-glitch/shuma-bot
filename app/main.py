"""Запуск бота: python -m app.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_config
from .db import Database
from .handlers import admin, client
from .sms import make_sender


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    if not cfg.bot_token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    if not cfg.admin_ids:
        logging.warning("ADMIN_IDS пуст: админка никому не доступна")
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(),
                    db=Database(cfg.db_path), cfg=cfg,
                    sms=make_sender(cfg.sms_provider, cfg.smsru_api_key))
    dp.include_router(admin.router)   # админ-роутер первым: его фильтр пропускает не-админов дальше
    dp.include_router(client.router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
