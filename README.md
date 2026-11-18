# Бот Шумы: договор, подпись по SMS, оплата

Клиент выбирает курс → один раз заполняет данные → получает договор и заявку в PDF →
подписывает кодом из SMS (простая электронная подпись) → получает ссылку на оплату и сумму →
нажимает «Я оплатил(а)» → админ подтверждает оплату в боте.

Админы (`ADMIN_IDS`) командой `/admin` создают курсы и индивидуальные занятия с любой ценой,
получают личные ссылки на них, скрывают старые, подтверждают оплаты и выгружают заказы в CSV.

## Структура

| Путь | Что там |
| --- | --- |
| `app/handlers/client.py` | Диалог клиента: регистрация, подпись, оплата |
| `app/handlers/admin.py` | Админка |
| `app/db.py` | База SQLite, включая журнал подписаний `signature_log` |
| `app/signing.py` | Одноразовые коды: 6 цифр, 10 минут, 5 попыток, хранится только хэш |
| `app/sms.py` | `console` — тестовый режим (код виден в чате), `smsru` — SMS.ru |
| `app/documents.py` | Шаблон → PDF с отметкой о ПЭП и SHA-256 текста |
| `templates/contract.txt` | Текст договора. **Заменить после правок юриста**; поля `{...}` подставляются ботом |
| `templates/application.txt` | Приложение № 1 «Заявка» |

## Запуск локально (тестовый режим)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env    # вписать BOT_TOKEN и ADMIN_IDS
.venv/bin/python -m pytest -q
.venv/bin/python -m app.main
```

## Запуск на сервере (VPS в России, Ubuntu)

```bash
sudo useradd -r -m -d /opt/shuma-bot bot
sudo -u bot git clone <репозиторий> /opt/shuma-bot   # или скопировать папку
cd /opt/shuma-bot && sudo -u bot python3 -m venv .venv && sudo -u bot .venv/bin/pip install -r requirements.txt
sudo -u bot cp .env.example .env && sudo -u bot nano .env   # SMS_PROVIDER=smsru, SMSRU_API_KEY=...
sudo cp deploy/shuma-bot.service /etc/systemd/system/ && sudo systemctl enable --now shuma-bot
```

Данные (база и подписанные PDF) лежат в `data/`. Их нужно регулярно копировать в резерв:
это доказательства подписания.

## Известные ограничения

- Состояние диалога хранится в памяти: если бот перезапустится посреди оформления, клиенту
  нужно нажать /start ещё раз. Подписанные документы и заказы при этом не теряются.
- Бот не видит оплату на сайте сам: подтверждение ручное (кнопка у админа).
