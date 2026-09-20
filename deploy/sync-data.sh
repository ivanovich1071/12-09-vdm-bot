#!/usr/bin/env bash
# Полная синхронизация данных бота: сайт → каталог → фото → разделы приказов.
#
# Запуск на сервере из /opt/vdm-bot:
#   bash deploy/sync-data.sh                 # обход сайта, каталог, разделы приказов
#   bash deploy/sync-data.sh --no-approve    # до утверждения: разбор и diff, без применения
#   bash deploy/sync-data.sh --no-site       # пропустить обход сайта (только фото/приказы)
#   bash deploy/sync-data.sh --media         # + сбор фото и характеристик (занимает часы:
#                                            #   тысячи карточек по одной в секунду)
#
# Каталог утверждается только после показа diff: у approve есть предохранители
# (>10% удалённых позиций или >30% изменившихся цен требуют явного --force), и
# автоматика мимо них не идёт. Разделы приказов пересобираются из PDF в корне
# проекта — в git и в docker-образ они не входят, их кладут в /opt/vdm-bot руками.
# Прежние версии данных остаются на диске: откат — `run.py catalog rollback`.
set -euo pipefail
cd "$(dirname "$0")/.."

APPROVE=1 WANT_SITE=1 WANT_MEDIA=0
for arg in "$@"; do
  case "$arg" in
    --no-approve) APPROVE=0 ;;
    --no-site) WANT_SITE=0 ;;
    --media) WANT_MEDIA=1 ;;
    *) echo "неизвестный флаг: $arg"; exit 2 ;;
  esac
done

RUN="docker compose --profile tools run --rm catalog"

if [ "$WANT_SITE" = 1 ]; then
  echo "=== 1/5 Прайс: обход сайта vdm.ru (выгрузка в формате 1С)"
  $RUN site --out "data/raw/site-$(date +%Y%m%d-%H%M).xlsx"

  echo "=== 2/5 Каталог: приём выгрузки и diff"
  LATEST=$(ls -t data/raw/site-*.xlsx | head -1)
  IMPORT=$($RUN import-1c --file "$LATEST" | sed -n 's/^Импорт \([^ ]*\) —.*/\1/p' | head -1)
  if [ -z "$IMPORT" ]; then
    echo "не удалось определить ID импорта — посмотрите: $RUN import-1c --list"
    exit 1
  fi
  $RUN import-1c --diff "$IMPORT" | tail -20
  if [ "$APPROVE" = 1 ]; then
    echo "=== 3/5 Каталог: утверждение импорта $IMPORT"
    $RUN catalog approve "$IMPORT"
  else
    echo "=== 3/5 Утверждение пропущено (--no-approve). Когда будете готовы:"
    echo "    $RUN catalog approve $IMPORT"
  fi
else
  echo "=== 1-3/5 Пропущено (--no-site)"
fi

if [ "$WANT_MEDIA" = 1 ]; then
  echo "=== 4/5 Фото и характеристики: сбор с сайта и публикация версии"
  $RUN media --listing --cards --sync
else
  echo "=== 4/5 Фото пропущены (запустите с --media)"
fi

echo "=== 5/5 Разделы приказов: пересборка из PDF и структурная проверка"
$RUN acts --check

echo "=== Перезапуск бота: нормативы и каталог подхватятся при старте"
docker compose up -d
docker compose ps
