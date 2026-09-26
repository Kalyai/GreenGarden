#!/usr/bin/env bash
# Восстановление заявок из шифрованного архива.
#
#   sudo bash deploy/restore.sh /var/backups/greengarden/greengarden-2026-09-25_03-1500.tar.gz.gpg
#   sudo bash deploy/restore.sh --yes <архив>     # без подтверждения (для скриптов)
#
# Порядок действий:
#   1. снимок текущего состояния тем же backup.sh — чтобы замену можно было откатить;
#   2. расшифровка архива во временный каталог;
#   3. остановка app (nginx продолжает отдавать сайт, приём заявок паузируется);
#   4. замена содержимого тома data и возврат прав владельцу;
#   5. запуск app и проверка числа заявок.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

KEY_FILE=$(env_get BACKUP_PASSPHRASE_FILE); KEY_FILE=${KEY_FILE:-/root/.greengarden-backup-key}
BACKUP_DIR=$(env_get BACKUP_DIR);           BACKUP_DIR=${BACKUP_DIR:-/var/backups/greengarden}

# Как и в backup.sh: проверяем фактические права, а не uid.
command -v gpg >/dev/null 2>&1 || die "не установлен gpg"
docker info >/dev/null 2>&1 || die "нет доступа к Docker: sudo usermod -aG docker $USER"
[ -r "$KEY_FILE" ] || die "не читается файл парольной фразы $KEY_FILE (нужен sudo)"

YES=0
[ "${1:-}" = "--yes" ] && { YES=1; shift; }
ARCHIVE=${1:-}
[ -n "$ARCHIVE" ] || die "укажите путь к архиву: bash deploy/restore.sh <файл.tar.gz.gpg>"
[ -f "$ARCHIVE" ] || die "файл не найден: $ARCHIVE"

# Временный каталог рядом с бэкапами: путь монтируется в контейнер, а его
# разбирает демон Docker, который не видит частный /tmp systemd-юнита.
mkdir -p "$BACKUP_DIR" 2>/dev/null || die "нет доступа к каталогу бэкапов $BACKUP_DIR (нужен sudo)"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
WORK=$(mktemp -d "$BACKUP_DIR/.restore-XXXXXX")
PLAIN="$WORK/snapshot.tar.gz"
trap 'shred -u "$PLAIN" 2>/dev/null || rm -f "$PLAIN"; rm -rf "$WORK"' EXIT

log "Шаг 1. Снимаю текущее состояние (чтобы замену можно было откатить)"
bash deploy/backup.sh >/dev/null
log "Снимок сохранён в $BACKUP_DIR"

log "Шаг 2. Расшифровываю $ARCHIVE"
gpg --batch --yes --decrypt --passphrase-file "$KEY_FILE" -o "$PLAIN" "$ARCHIVE"
# Чтение для пользователя контейнера: gpg создаёт файл с правами 0600 root,
# а распаковывает его uid 10001. Каталог $WORK остаётся 0700, а снимок
# уничтожается в trap при любом выходе из скрипта.
chmod 644 "$PLAIN"
FILES=$(tar -tzf "$PLAIN" | grep -v '^\./$' | tr '\n' ' ')
log "В архиве: ${FILES:-пусто}"

if [ "$YES" != 1 ]; then
    printf '\nТекущие заявки в томе data будут ЗАМЕНЕНЫ содержимым архива.\n'
    printf 'Продолжить? Напишите «да»: '
    read -r ANSWER
    [ "$ANSWER" = "да" ] || die "отменено пользователем"
fi

log "Шаг 3. Останавливаю app"
"${COMPOSE[@]}" stop app >/dev/null

log "Шаг 4. Заменяю содержимое тома"
# От имени владельца данных (uid 10001 — пользователь gd из Dockerfile):
# сервис объявлен с cap_drop ALL, поэтому root внутри контейнера не смог бы
# прочитать /data. Созданные этим пользователем файлы и так принадлежат ему,
# отдельный chown не нужен.
"${COMPOSE[@]}" run --rm --no-deps -T --user 10001:10001 -v "$WORK:/restore" app sh -c '
    set -e
    find /data -mindepth 1 -delete
    tar -C /data -xzf /restore/snapshot.tar.gz
    chmod 700 /data
    find /data -type f -name "*.json" -exec chmod 600 {} +
'

log "Шаг 5. Запускаю app"
"${COMPOSE[@]}" start app >/dev/null
sleep 8

COUNT=$("${COMPOSE[@]}" exec -T app python -c \
    "import json;print(len(json.load(open('/data/leads.json'))))" 2>/dev/null || echo '?')
log "Готово: в базе $COUNT заявок"
warn "Проверьте сайт: bash deploy/verify.sh, и отправьте тестовую заявку."
