#!/usr/bin/env bash
# Установка Telegram Video Panel на Ubuntu (22.04 / 24.04).
#
#   sudo bash install.sh                   — без домена: панель доступна через SSH-туннель
#   sudo bash install.sh --ip              — по IP: http://<IP сервера>:8765 (вход по паролю панели)
#   sudo bash install.sh video.example.com — с доменом: HTTPS через Caddy (сертификат автоматически)
#
# Повторный запуск обновляет код и сохраняет пароль, вход в Telegram и скачанные видео.
set -euo pipefail

DOMAIN="${1:-}"
if [ "$DOMAIN" = "-h" ] || [ "$DOMAIN" = "--help" ]; then sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0; fi
BIND=127.0.0.1
if [ "$DOMAIN" = "--ip" ]; then BIND=0.0.0.0; DOMAIN=""; fi
APP_DIR=/opt/tg-video-panel
DATA_DIR=/var/lib/tg-video-panel
ENV_FILE=/etc/tg-video-panel.env
SVC=tg-video-panel
PORT=8765
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo: sudo bash install.sh $DOMAIN"; exit 1; }
[ -f "$SRC/app.py" ] || { echo "Рядом с install.sh должен лежать app.py"; exit 1; }

echo "==> Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || echo "   (apt-get update завершился с ошибкой — продолжаю с имеющимися списками пакетов)"
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg curl >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Нужен Python 3.9 или новее (Ubuntu 22.04+)"; exit 1; }

echo "==> Пользователь и папки"
id tvp >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin tvp
mkdir -p "$APP_DIR" "$DATA_DIR/videos"
install -m 644 "$SRC/app.py" "$SRC/requirements.txt" "$APP_DIR/"
chown -R tvp:tvp "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Python-окружение"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" install -q cryptg 2>/dev/null || echo "   (cryptg не установился — скачивание будет чуть медленнее, это не критично)"

echo "==> Настройки"
if [ ! -f "$ENV_FILE" ]; then
  PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"
  cat > "$ENV_FILE" <<CFG
TVP_SERVER=1
TVP_HOST=$BIND
TVP_PORT=$PORT
TVP_PASSWORD=$PASS
TVP_DATA_DIR=$DATA_DIR
TVP_DOWNLOAD_DIR=$DATA_DIR/videos
CFG
  chmod 600 "$ENV_FILE"
fi
sed -i "s/^TVP_HOST=.*/TVP_HOST=$BIND/" "$ENV_FILE"
PASS="$(grep '^TVP_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"

echo "==> Служба systemd"
cat > /etc/systemd/system/$SVC.service <<UNIT
[Unit]
Description=Telegram Video Panel
After=network-online.target
Wants=network-online.target

[Service]
User=tvp
Group=tvp
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/app.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now $SVC >/dev/null
systemctl restart $SVC

# Добавляет панель в Caddyfile, не ломая чужие сайты: стандартный файл из пакета заменяем,
# свой — дополняем блоком (с копией и проверкой конфигурации).
caddy_site() {
  local cf="${CADDYFILE:-/etc/caddy/Caddyfile}" backup=""
  local block="# tg-video-panel
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT
}"
  if [ -f "$cf" ] && grep -q "^# tg-video-panel" "$cf"; then
    echo "   блок панели уже есть в $cf — не трогаю"
    return 0
  fi
  if [ -f "$cf" ]; then
    backup="$cf.bak-$(date +%Y%m%d-%H%M%S)"
    cp -a "$cf" "$backup" && echo "   копия: $backup"
  fi
  if [ ! -s "$cf" ] || { grep -q "/usr/share/caddy" "$cf" && [ "$(grep -cvE '^[[:space:]]*(#|$)' "$cf")" -le 6 ]; }; then
    printf '%s\n' "$block" > "$cf"      # стандартный Caddyfile из пакета — заменяем
  else
    printf '\n%s\n' "$block" >> "$cf"   # свой Caddyfile — дописываем блок
  fi
  if command -v caddy >/dev/null && ! caddy validate --config "$cf" --adapter caddyfile >/dev/null 2>&1; then
    echo "   Caddyfile с ошибкой — возвращаю копию"
    [ -n "$backup" ] && cp -a "$backup" "$cf"
    return 1
  fi
}

if [ -n "$DOMAIN" ]; then
  echo "==> Caddy (HTTPS для $DOMAIN)"
  command -v caddy >/dev/null || apt-get install -y -qq caddy >/dev/null \
    || { echo "Не удалось установить Caddy из репозитория Ubuntu (apt install caddy)"; exit 1; }
  caddy_site
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  fi
elif [ "$BIND" = "0.0.0.0" ] && command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow $PORT/tcp >/dev/null && echo "==> Порт $PORT открыт в ufw"
fi

sleep 2
if systemctl is-active --quiet $SVC; then STATUS="работает"; else STATUS="НЕ запустилась — смотрите: journalctl -u $SVC -n 50"; fi
IP="$(curl -s4 --max-time 3 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"

echo
echo "================================================================"
VER="$(grep -m1 '^VERSION = ' "$APP_DIR/app.py" | cut -d'"' -f2)"
echo " Telegram Video Panel v$VER: $STATUS"
echo " Пароль панели:  $PASS"
if [ -n "$DOMAIN" ]; then
  echo " Адрес:          https://$DOMAIN"
  echo " (DNS-запись A для $DOMAIN должна указывать на $IP)"
elif [ "$BIND" = "0.0.0.0" ]; then
  echo " Адрес:          http://$IP:$PORT"
  echo " (соединение без шифрования; для HTTPS установите с доменом: sudo bash install.sh ваш.домен)"
else
  echo " Подключение с вашего компьютера (оставьте окно открытым):"
  echo "   ssh -N -L $PORT:127.0.0.1:$PORT $(logname 2>/dev/null || echo root)@$IP"
  echo " затем откройте  http://127.0.0.1:$PORT"
fi
echo " Видео на сервере: $DATA_DIR/videos"
echo " Управление: systemctl status|restart|stop $SVC ; логи: journalctl -u $SVC -f"
echo " Сменить пароль: sudo nano $ENV_FILE && sudo systemctl restart $SVC"
echo "================================================================"
echo
journalctl -u $SVC -n 15 --no-pager || true
