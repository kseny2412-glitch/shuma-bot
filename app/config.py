"""Настройки бота из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Executor:
    name: str = ""
    inn: str = ""
    ogrnip: str = ""
    city: str = ""


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    payment_url: str
    sms_provider: str
    smsru_api_key: str
    data_dir: Path
    timezone: str = "Europe/Moscow"
    executor: Executor = field(default_factory=Executor)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.sqlite3"

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "documents"


def load_config() -> Config:
    _load_dotenv(ROOT / ".env")
    admins = frozenset(
        int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    return Config(
        bot_token=os.environ.get("BOT_TOKEN", ""),
        admin_ids=admins,
        payment_url=os.environ.get("PAYMENT_URL", "https://shumkadance.tb.ru/"),
        sms_provider=os.environ.get("SMS_PROVIDER", "console"),
        smsru_api_key=os.environ.get("SMSRU_API_KEY", ""),
        data_dir=data_dir,
        timezone=os.environ.get("TIMEZONE", "Europe/Moscow"),
        executor=Executor(
            name=os.environ.get("EXECUTOR_NAME", ""),
            inn=os.environ.get("EXECUTOR_INN", ""),
            ogrnip=os.environ.get("EXECUTOR_OGRNIP", ""),
            city=os.environ.get("EXECUTOR_CITY", ""),
        ),
    )
