"""Отправка кода подписи. console — тестовый режим, smsru — SMS.ru."""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger(__name__)


class SmsError(Exception):
    pass


class SmsSender:
    name = "base"
    #: True — код нужно показать пользователю в чате (только для теста)
    shows_code_in_chat = False

    async def send(self, phone: str, text: str) -> None:  # pragma: no cover
        raise NotImplementedError


class ConsoleSender(SmsSender):
    name = "console"
    shows_code_in_chat = True

    async def send(self, phone: str, text: str) -> None:
        log.warning("ТЕСТОВЫЙ РЕЖИМ: SMS на %s не отправлено: %s", phone, text)


class SmsRuSender(SmsSender):
    name = "smsru"
    URL = "https://sms.ru/sms/send"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("SMSRU_API_KEY не задан")
        self.api_key = api_key

    async def send(self, phone: str, text: str) -> None:
        params = {"api_id": self.api_key, "to": phone.lstrip("+"), "msg": text, "json": 1}
        async with aiohttp.ClientSession() as s:
            async with s.post(self.URL, data=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                data = await r.json(content_type=None)
        if data.get("status") != "OK":
            raise SmsError(f"SMS.ru: {data}")
        sms = next(iter(data.get("sms", {}).values()), {})
        if sms.get("status") != "OK":
            raise SmsError(f"SMS.ru: {sms}")


def make_sender(provider: str, api_key: str = "") -> SmsSender:
    if provider == "smsru":
        return SmsRuSender(api_key)
    return ConsoleSender()
