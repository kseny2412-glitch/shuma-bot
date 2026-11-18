import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, Executor  # noqa: E402
from app.db import Client, Database  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return Config(bot_token="x", admin_ids=frozenset({1}), payment_url="https://shumkadance.tb.ru/",
                  sms_provider="console", smsru_api_key="", data_dir=tmp_path,
                  executor=Executor("Иванова Анна Сергеевна", "123456789012", "123456789012345", "Калининград"))


@pytest.fixture
def db(cfg):
    return Database(cfg.db_path)


@pytest.fixture
def client(db):
    c = Client(42, "Петрова Мария Ивановна", "05.03.1995", "+79990001122", "m@example.com", True)
    db.save_client(c)
    return c
