#!/usr/bin/env bash
# Установка бота Шумы на чистый сервер Ubuntu 22.04/24.04. Запускать от root:
#   bash install.sh
# Скрипт сам спросит токен бота, Telegram ID админов, ключ SMS.ru и реквизиты ИП.
# Повторный запуск обновляет код и перезапускает бота, не трогая данные и .env.
set -euo pipefail

REPO="github.com/kseny2412-glitch/shuma-bot"
DIR=/opt/shuma-bot
SERVICE=shuma-bot

if [[ $EUID -ne 0 ]]; then echo "Запустите от root"; exit 1; fi

echo "== Устанавливаю системные пакеты"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv >/dev/null

id bot >/dev/null 2>&1 || useradd -r -m -d "$DIR" -s /usr/sbin/nologin bot

echo "== Скачиваю код"
if [[ -d "$DIR/.git" ]]; then
  sudo -u bot git -C "$DIR" pull --ff-only
else
  if git ls-remote "https://$REPO" >/dev/null 2>&1; then
    URL="https://$REPO"
  else
    echo "Репозиторий закрытый. Вставьте GitHub-токен с доступом только на чтение к shuma-bot"
    read -rsp "Токен (ввод не отображается): " GH_TOKEN; echo
    URL="https://x-access-token:${GH_TOKEN}@$REPO"
  fi
  rm -rf "$DIR.tmp" && git clone -q "$URL" "$DIR.tmp"
  git -C "$DIR.tmp" remote set-url origin "https://$REPO"   # токен не остаётся на диске
  cp -a "$DIR.tmp/." "$DIR/" && rm -rf "$DIR.tmp"
  chown -R bot:bot "$DIR"
fi

echo "== Ставлю зависимости Python"
sudo -u bot python3 -m venv "$DIR/.venv"
sudo -u bot "$DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u bot "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [[ ! -f "$DIR/.env" ]]; then
  echo "== Настройка. Секреты при вводе не отображаются."
  read -rsp "Токен бота от @BotFather: " BOT_TOKEN; echo
  read -rp  "Telegram ID админов через запятую (Шума, Ксеня): " ADMIN_IDS
  read -rsp "API-ключ SMS.ru (пусто = тестовый режим, код в чате): " SMSRU; echo
  read -rp  "ФИО ИП полностью: " EX_NAME
  read -rp  "ИНН ИП: " EX_INN
  read -rp  "ОГРНИП: " EX_OGRN
  read -rp  "Город для договора (без «г.»): " EX_CITY
  read -rp  "Часовой пояс [Europe/Moscow]: " TZ_IN
  PROVIDER=console; [[ -n "$SMSRU" ]] && PROVIDER=smsru
  umask 077
  cat > "$DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
PAYMENT_URL=https://shumkadance.tb.ru/
SMS_PROVIDER=$PROVIDER
SMSRU_API_KEY=$SMSRU
DATA_DIR=./data
EXECUTOR_NAME=$EX_NAME
EXECUTOR_INN=$EX_INN
EXECUTOR_OGRNIP=$EX_OGRN
EXECUTOR_CITY=$EX_CITY
TIMEZONE=${TZ_IN:-Europe/Moscow}
EOF
  chown bot:bot "$DIR/.env"
fi

echo "== Запускаю бота как службу"
cp "$DIR/deploy/shuma-bot.service" /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable -q $SERVICE
systemctl restart $SERVICE
sleep 3
if systemctl is-active -q $SERVICE; then
  echo "✅ Бот работает. Напишите ему /start в Telegram, админам — /admin."
else
  echo "❌ Бот не запустился. Последние строки журнала:"
  journalctl -u $SERVICE -n 20 --no-pager
fi
