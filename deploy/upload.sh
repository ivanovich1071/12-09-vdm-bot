#!/usr/bin/env bash
# ============================================================================
#  upload.sh — привезти на сервер исходники и каталог, запустить установку
# ----------------------------------------------------------------------------
#  Запускать с машины разработки (Windows: Git Bash; Linux/Mac: обычный терминал):
#
#      bash deploy/upload.sh 135.106.222.191
#      DRY_RUN=1 bash deploy/upload.sh 135.106.222.191   # только собрать архивы
#
#  Репозиторий приватный, поэтому код едет не клоном, а архивом последнего
#  коммита: токен GitHub на сервер класть не нужно.
#  Каталог товаров (data/kb, база импорта) в git не хранится — везём отдельно.
#
#  Что НЕ уезжает: .env и ключи, data/vdm.sqlite3 (корзины и контакты клиентов),
#  журналы диалогов, предзаказы, стенограммы автотеста, фотографии товаров.
# ============================================================================
set -Eeuo pipefail

HOST="${1:-${VDM_HOST:-}}"
SSH_USER="${SSH_USER:-root}"
APP_DIR="${APP_DIR:-/opt/vdm-bot}"
BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

if [[ -z "$HOST" ]]; then
  echo "Укажите адрес сервера: bash deploy/upload.sh 135.106.222.191" >&2
  exit 1
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "[1/4] архив исходников: ветка ${BRANCH}, коммит $(git rev-parse --short HEAD)"
# Едет коммит, а не рабочая копия: на сервере должно оказаться ровно то, что лежит
# в репозитории. Несохранённые правки молча остались бы дома — предупреждаем.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "      ВНИМАНИЕ: есть незакоммиченные правки, на сервер они НЕ поедут:" >&2
  git status --short --untracked-files=no | sed 's/^/        /' >&2
fi
git archive --format=tar.gz -o "${WORK}/code.tgz" HEAD

echo "[2/4] архив каталога"
[[ -f data/kb/current ]] || { echo "нет data/kb/current — сначала соберите каталог локально" >&2; exit 1; }
# Версия, на которой стоит бот: её единственную и везём, остальные на сервере не нужны.
VERSION=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' data/kb/current)
[[ -n "$VERSION" ]] || { echo "не разобрал версию в data/kb/current" >&2; exit 1; }
echo "      версия каталога: ${VERSION}"

CATALOG_FILES=(
  data/kb/current
  "data/kb/versions/${VERSION}"
  data/kb/norm_items.json
  data/kb/norms_1057.json
)
# Реестр версий каталога: без него на сервере не сработают `catalog versions`,
# `approve` и откат. Файл крупный (~47 МБ), но едет один раз.
[[ -f data/catalog.sqlite3 ]] && CATALOG_FILES+=(data/catalog.sqlite3)
# Каталог до перехода на версии: страховка, если указатель окажется битым.
[[ -f data/kb/products.jsonl ]] && CATALOG_FILES+=(data/kb/products.jsonl)

tar -czf "${WORK}/catalog.tgz" "${CATALOG_FILES[@]}"
du -h "${WORK}/code.tgz" "${WORK}/catalog.tgz" | sed 's/^/      /'

if [[ -n "${DRY_RUN:-}" ]]; then
  echo "DRY_RUN — архивы собраны, на сервер ничего не отправлено. Содержимое каталога:"
  tar -tzf "${WORK}/catalog.tgz" | sed 's/^/      /'
  exit 0
fi

echo "[3/4] копирование на ${SSH_USER}@${HOST} (спросит пароль)"
ssh "${SSH_USER}@${HOST}" "mkdir -p '${APP_DIR}'"
scp "${WORK}/code.tgz" "${WORK}/catalog.tgz" "${SSH_USER}@${HOST}:${APP_DIR}/"

echo "[4/4] распаковка и установка на сервере"
ssh "${SSH_USER}@${HOST}" "bash -s" <<EOF
set -Eeuo pipefail
cd '${APP_DIR}'
tar -xzf code.tgz && rm -f code.tgz
tar -xzf catalog.tgz && rm -f catalog.tgz
chmod +x deploy/*.sh
bash deploy/install.sh
EOF

echo
echo "Готово. Если .env создался пустым — впишите ключи и перезапустите бота:"
echo "  ssh ${SSH_USER}@${HOST}"
echo "  nano ${APP_DIR}/.env"
echo "  cd ${APP_DIR} && docker compose restart"
