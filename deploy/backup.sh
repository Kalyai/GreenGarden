#!/usr/bin/env bash
# Шифрованная резервная копия заявок и состояний чатов.
#
#   sudo bash deploy/backup.sh
#
# Копируется содержимое тома data (leads.json, chats.json). Архив шифруется
# симметрично gpg: заявки — персональные данные (152-ФЗ), и копия на чужом
# хостинге без шифрования была бы утечкой.
#
# Сайт не останавливается: приложение пишет JSON через временный файл и
# os.replace, поэтому в архив попадает либо старая, либо новая версия целиком.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

BACKUP_DIR=$(env_get BACKUP_DIR);         BACKUP_DIR=${BACKUP_DIR:-/var/backups/greengarden}
KEY_FILE=$(env_get BACKUP_PASSPHRASE_FILE); KEY_FILE=${KEY_FILE:-/root/.greengarden-backup-key}
KEEP_DAYS=$(env_get BACKUP_KEEP_DAYS);     KEEP_DAYS=${KEEP_DAYS:-14}

# Права проверяем по факту, а не по uid: нужны доступ к Docker, каталогу
# бэкапов и файлу парольной фразы. На сервере они принадлежат root, поэтому
# обычно скрипт и запускают через sudo, — но отдельный пользователь бэкапов
# с теми же правами тоже подойдёт.
command -v gpg >/dev/null 2>&1 || die "не установлен gpg: apt-get install -y gpg"
docker info >/dev/null 2>&1 || die "нет доступа к Docker: sudo usermod -aG docker $USER"
[ -r "$KEY_FILE" ] || die "не читается файл парольной фразы $KEY_FILE (нужен sudo)"
mkdir -p "$BACKUP_DIR" 2>/dev/null || die "не создать каталог бэкапов $BACKUP_DIR (нужен sudo)"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

# Временный каталог внутри BACKUP_DIR, а не в /tmp: путь монтируется в
# контейнер, а его разбирает демон Docker, который частного /tmp юнита не видит.
STAMP=$(date +%F_%H-%M%S)
WORK=$(mktemp -d "$BACKUP_DIR/.work-XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# Файл снимка создаём заранее и даём на него запись пользователю контейнера.
# Сам каталог остаётся 0700 внутри BACKUP_DIR, снимок живёт считаные секунды
# и уничтожается shred сразу после шифрования.
install -m 666 /dev/null "$WORK/snapshot.tar.gz"

log "Снимаю том data в архив"
# Запуск от имени владельца данных (uid 10001 — пользователь gd из Dockerfile),
# а не от root: сервис объявлен с cap_drop ALL, поэтому даже root внутри
# контейнера не смог бы прочитать /data с правами 0700 чужого владельца
# (не хватает CAP_DAC_OVERRIDE).
# --no-deps — nginx для бэкапа поднимать не нужно.
"${COMPOSE[@]}" run --rm --no-deps -T --user 10001:10001 -v "$WORK:/dump" app \
    tar -C /data -czf /dump/snapshot.tar.gz .

OUT="$BACKUP_DIR/greengarden-$STAMP.tar.gz.gpg"
gpg --batch --yes --symmetric --cipher-algo AES256 \
    --passphrase-file "$KEY_FILE" -o "$OUT" "$WORK/snapshot.tar.gz"
chmod 600 "$OUT"

# Открытый архив не должен остаться на диске даже на время работы скрипта.
shred -u "$WORK/snapshot.tar.gz" 2>/dev/null || rm -f "$WORK/snapshot.tar.gz"

# Хранение: оставляем только свежие копии
find "$BACKUP_DIR" -maxdepth 1 -name 'greengarden-*.tar.gz.gpg' -mtime +"$KEEP_DAYS" -delete

SIZE=$(du -h "$OUT" | cut -f1)
COUNT=$(find "$BACKUP_DIR" -maxdepth 1 -name 'greengarden-*.tar.gz.gpg' | wc -l | tr -d ' ')
log "Готово: $OUT ($SIZE), всего копий: $COUNT, хранятся $KEEP_DAYS дн."

# Проверка целостности: без неё бэкап считается рабочим только на словах.
if gpg --batch --decrypt --passphrase-file "$KEY_FILE" "$OUT" 2>/dev/null \
   | tar -tzf - >/dev/null 2>&1; then
    log "Архив расшифровывается и читается — копия пригодна"
else
    die "архив не прошёл проверку: расшифровать или прочитать его не удалось"
fi

cat <<EOF

Локальная копия — не резервная копия: при потере сервера она исчезнет вместе
с ним. Настройте выгрузку $BACKUP_DIR во внешнее хранилище (S3, Backblaze,
другой сервер) и проверьте восстановление командой deploy/restore.sh.
EOF
