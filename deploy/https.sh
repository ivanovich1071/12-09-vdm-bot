#!/usr/bin/env bash
# ============================================================================
#  https.sh — домен и сертификат для виджета
# ----------------------------------------------------------------------------
#  nginx становится перед uvicorn, Let's Encrypt выдаёт сертификат, http
#  переезжает на https. Запускать на сервере от root:
#
#      bash deploy/https.sh 135-106-222-191.sslip.io            # без почты
#      bash deploy/https.sh bot.vdm.ru admin@vdm.ru             # с почтой
#
#  Смена домена — повторный запуск с новым именем: старый конфиг заменяется,
#  сертификат выпускается заново.
#
#  Домен должен указывать на этот сервер ДО запуска: Let's Encrypt проверяет
#  владение, запрашивая файл по http://<домен>/.well-known/…
# ============================================================================
set -Eeuo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIDGET_PORT="${WIDGET_PORT:-8000}"
# Заказ клиента приходит файлом до 20 МБ (ORDER_UPLOAD_MAX_MB), у nginx по
# умолчанию предел 1 МБ — без этой строки загрузка падала бы с 413.
UPLOAD_LIMIT="${UPLOAD_LIMIT:-25m}"

GRN=$'\033[0;32m'; YEL=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo "${GRN}OK${NC} $*"; }
warn() { echo "${YEL}!${NC} $*"; }

[[ -n "$DOMAIN" ]] || { echo "Укажите домен: bash deploy/https.sh <домен> [почта]" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Запускайте от root" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive

echo "[1/6] проверка домена"
RESOLVED=$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)
MYIP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo "      ${DOMAIN} → ${RESOLVED:-не резолвится}, сервер → ${MYIP}"
[[ "$RESOLVED" == "$MYIP" ]] || warn "домен указывает не на этот сервер — Let's Encrypt откажет"

echo "[2/6] nginx и certbot"
apt-get update -yqq
apt-get install -yqq --no-install-recommends nginx certbot python3-certbot-nginx
ok "установлены"

echo "[3/6] порты 80 и 443"
if command -v ufw >/dev/null; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
  # Порт приложения наружу больше не нужен: снаружи смотрит nginx. Заодно это
  # условие, при котором uvicorn доверяет заголовкам X-Forwarded-* (web/app.py).
  ufw delete allow "${WIDGET_PORT}/tcp" >/dev/null 2>&1 || true
  ok "открыты 80 и 443, порт ${WIDGET_PORT} закрыт снаружи"
fi

echo "[4/6] конфигурация nginx"
cat > /etc/nginx/sites-available/vdm-bot <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    # Заказ клиента файлом: .xlsx, .docx, .pdf до 20 МБ.
    client_max_body_size ${UPLOAD_LIMIT};

    location / {
        proxy_pass http://127.0.0.1:${WIDGET_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Ответ модели идёт до минуты (LLM_TIMEOUT_SECONDS=60), плюс сборка
        # спецификации. Со стандартными 60 с nginx рвал бы длинные ответы.
        proxy_connect_timeout 15s;
        proxy_send_timeout 180s;
        proxy_read_timeout 180s;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/vdm-bot /etc/nginx/sites-enabled/vdm-bot
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
ok "nginx проксирует ${DOMAIN} на 127.0.0.1:${WIDGET_PORT}"

echo "[5/6] сертификат Let's Encrypt"
CERTBOT_ARGS=(--nginx -d "$DOMAIN" --agree-tos --redirect --non-interactive)
if [[ -n "$EMAIL" ]]; then
  CERTBOT_ARGS+=(-m "$EMAIL" --no-eff-email)
else
  CERTBOT_ARGS+=(--register-unsafely-without-email)
fi
certbot "${CERTBOT_ARGS[@]}"
systemctl reload nginx
ok "сертификат выпущен, http переведён на https"

echo "[6/6] домен в списке разрешённых источников виджета"
cd "$APP_DIR"
ORIGIN="https://${DOMAIN}"
CURRENT=$(grep -m1 "^WIDGET_ALLOWED_ORIGINS=" .env | cut -d= -f2-)
if [[ ",$CURRENT," != *",$ORIGIN,"* ]]; then
  sed -i "s#^WIDGET_ALLOWED_ORIGINS=.*#WIDGET_ALLOWED_ORIGINS=${CURRENT:+$CURRENT,}${ORIGIN}#" .env
  docker compose restart widget >/dev/null
  ok "добавлен ${ORIGIN}, виджет перезапущен"
else
  ok "${ORIGIN} уже в списке"
fi

echo
curl -fsS --max-time 15 "https://${DOMAIN}/health" && echo
cat <<EOF

${GRN}=============================================================${NC}
 Виджет   : https://${DOMAIN}/
 Демо     : https://${DOMAIN}/demo
 Здоровье : https://${DOMAIN}/health

 Сертификат продлевается сам (таймер certbot.timer).
 Проверить продление: certbot renew --dry-run
${GRN}=============================================================${NC}
EOF
