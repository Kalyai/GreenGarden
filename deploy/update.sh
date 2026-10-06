#!/usr/bin/env bash
# Обновление сайта: git pull -> пересборка образов -> плавный перезапуск -> проверки.
#
#   sudo bash deploy/update.sh              # с обновлением репозитория
#   bash deploy/update.sh --no-pull         # пересобрать текущее дерево
#
# Требование чек-листа аудита: HTML, js/main.js и backend/app.py выкатываются
# одновременно — F-09 меняет контракт между формой и /api/lead, и при рассинхроне
# посетитель увидел бы «спасибо за заявку», которая не сохранилась. Здесь это
# гарантировано конструкцией: оба образа собираются из одного рабочего дерева,
# а статика попадает в nginx через стадию static одного Dockerfile.
#
# Простои: перезапуск контейнеров занимает 1–3 секунды. Аудит рекомендует
# выкатывать обновления в часы низкого трафика.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

docker info >/dev/null 2>&1 || die "нет доступа к Docker: sudo usermod -aG docker $USER (и повторный вход)"
require_env DOMAIN
[ -f .env ] && chmod 600 .env 2>/dev/null || true

PULL=1
[ "${1:-}" = "--no-pull" ] && PULL=0

if [ "$PULL" = 1 ]; then
    [ -d .git ] || die "это не git-репозиторий — используйте --no-pull"
    if [ -n "$(git status --porcelain)" ]; then
        warn "В рабочем дереве есть незакоммиченные правки — они останутся на месте"
        git status --short | head -10
    fi
    BEFORE=$(git rev-parse --short HEAD)
    log "Обновляю репозиторий"
    git fetch --prune origin
    git pull --ff-only
    AFTER=$(git rev-parse --short HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        log "Изменений нет ($AFTER) — пересборка всё равно будет выполнена"
    else
        log "Обновлено: $BEFORE -> $AFTER"
        git log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
    fi
fi

log "Проверяю синтаксис Python перед сборкой"
python3 -m py_compile backend/*.py tools/*.py 2>/dev/null \
    || die "Python-код не компилируется — выкатка остановлена"

log "Собираю образы (--pull: свежие базовые образы закрывают A06)"
"${COMPOSE[@]}" build --pull

log "Перезапускаю контейнеры"
"${COMPOSE[@]}" up -d

# Образы прежних сборок больше не нужны; тома и сети не трогаем.
docker image prune -f >/dev/null 2>&1 || true

log "Жду готовности и проверяю сайт"
sleep 15
if bash deploy/verify.sh; then
    log "Обновление завершено: сайт отвечает"
else
    warn "Проверки не прошли. Откат: git checkout <прежний коммит> && bash deploy/update.sh --no-pull"
    exit 1
fi
