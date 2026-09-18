#!/usr/bin/env bash
# ============================================================================
#  install.sh — поднять бота на чистой Ubuntu 22.04/24.04 (Selectel, Cloud.ru — любая ВМ)
# ----------------------------------------------------------------------------
#  Скрипт лежит внутри проекта: каталог проекта — на уровень выше скрипта.
#  Исходники привозит deploy/upload.sh с машины разработки, поэтому здесь нет
#  ни клонирования приватного репозитория, ни токенов GitHub.
#
#  Запуск на сервере:
#      cd /opt/vdm-bot && bash deploy/install.sh
#
#  Идемпотентно: повторный запуск пропускает уже сделанное, .env не перетирает.
# ============================================================================
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIDGET_PORT="${WIDGET_PORT:-8000}"
TZ_NAME="${TZ_NAME:-Europe/Moscow}"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

BLU=$'\033[0;34m'; GRN=$'\033[0;32m'; YEL=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
log()  { echo "${BLU}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo "${GRN}[$(date +%H:%M:%S)] OK${NC} $*"; }
warn() { echo "${YEL}[$(date +%H:%M:%S)] !${NC} $*"; }
err()  { echo "${RED}[$(date +%H:%M:%S)] x${NC} $*" >&2; }
trap 'rc=$?; err "строка $LINENO, код $rc"; exit $rc' ERR

[[ $EUID -eq 0 ]] || { err "запускайте от root: sudo bash deploy/install.sh"; exit 1; }
grep -qi ubuntu /etc/os-release || warn "ожидалась Ubuntu, у вас: $(. /etc/os-release && echo "$PRETTY_NAME")"

# ----------------------------------------------------------------------------
# 1. Базовые пакеты и время
# ----------------------------------------------------------------------------
log "1/6 базовые пакеты"
apt-get update -yqq
apt-get install -yqq --no-install-recommends ca-certificates curl git jq ufw tzdata
timedatectl set-timezone "$TZ_NAME" 2>/dev/null || true
ok "пакеты на месте, время: $(date '+%d.%m %H:%M %Z')"

# ----------------------------------------------------------------------------
# 2. Docker CE + Compose v2
# ----------------------------------------------------------------------------
log "2/6 Docker"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "уже стоит: $(docker --version), compose $(docker compose version --short)"
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg |
    gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -yqq
  apt-get install -yqq --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  ok "$(docker --version), compose $(docker compose version --short)"
fi

# ----------------------------------------------------------------------------
# 3. Файрвол: наружу открыты только SSH и порт виджета
# ----------------------------------------------------------------------------
log "3/6 ufw"
ufw allow 22/tcp comment 'SSH' >/dev/null
ufw allow "${WIDGET_PORT}/tcp" comment 'vdm widget' >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
if ufw --force enable >/dev/null 2>&1; then
  ok "открыты 22 и ${WIDGET_PORT}"
else
  warn "ufw не включился (бывает на контейнерных ВМ) — закройте порты средствами облака"
fi

# ----------------------------------------------------------------------------
# 4. Каталоги данных и .env
# ----------------------------------------------------------------------------
log "4/6 каталоги и .env"
cd "$APP_DIR"
mkdir -p data/kb data/raw data/uploads data/preorders data/orders data/dialogs secrets
chmod 700 secrets

if [[ -f .env ]]; then
  ok ".env уже есть — не трогаю"
else
  cp .env.example .env
  chmod 600 .env
  warn ".env создан из шаблона. Ключи впишите руками: nano ${APP_DIR}/.env"
  warn "нужны TELEGRAM_BOT_TOKEN и хотя бы один из CLOUDRU_API_KEY / OPENROUTER_API_KEY"
fi

if [[ -f data/kb/current ]]; then
  ok "каталог на месте: $(jq -r '.version' data/kb/current 2>/dev/null || echo '?')"
else
  warn "нет data/kb/current — каталога товаров у бота не будет."
  warn "привезите его: bash deploy/upload.sh (с машины разработки)"
fi

# ----------------------------------------------------------------------------
# 5. Сборка и запуск
# ----------------------------------------------------------------------------
log "5/6 сборка образа и запуск"
docker compose build --pull
docker compose up -d
ok "контейнеры подняты"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' || true

# ----------------------------------------------------------------------------
# 6. Проверки
# ----------------------------------------------------------------------------
log "6/6 проверки"
HEALTH=""
for _ in $(seq 1 20); do
  if HEALTH=$(curl -fsS --max-time 3 "http://127.0.0.1:${WIDGET_PORT}/health" 2>/dev/null); then
    break
  fi
  sleep 3
done
if [[ -n "$HEALTH" ]]; then
  ok "виджет отвечает: $HEALTH"
else
  warn "виджет молчит 60 с — docker compose logs widget"
fi

TG_STATE=$(docker compose ps --format json 2>/dev/null |
  jq -rs 'flatten | .[] | select(.Service=="telegram") | .State' 2>/dev/null || echo "?")
if [[ "$TG_STATE" == "running" ]]; then
  ok "Telegram-контейнер работает"
else
  warn "Telegram в состоянии '${TG_STATE}' — docker compose logs telegram"
  warn "частая причина: пустой TELEGRAM_BOT_TOKEN или тот же токен уже опрашивается с другой машины"
fi

IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
cat <<EOF

${GRN}=============================================================${NC}
 Проект   : ${APP_DIR}
 Виджет   : http://${IP}:${WIDGET_PORT}/         (демо: /demo)
 Здоровье : http://${IP}:${WIDGET_PORT}/health

 Дальше (из ${APP_DIR}):
   docker compose logs -f --tail=100 telegram   # что отвечает бот
   docker compose restart telegram              # после правки .env
   docker compose --profile tools run --rm catalog llm        # проверить доступ к модели
   docker compose run --rm telegram python run.py telegram --check
${GRN}=============================================================${NC}
EOF
