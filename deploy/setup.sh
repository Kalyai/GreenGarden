#!/usr/bin/env bash
# Первичная настройка сервера и первый запуск сайта «Зелёный дворик».
# ОС: Ubuntu 22.04/24.04 или Debian 12.
#
#   cd /opt/greengarden && sudo bash deploy/setup.sh
#
# Что делает (идемпотентно — повторный запуск не ломает работающий сайт):
#   1. Docker CE + compose-плагин из официального репозитория;
#   2. .env: создаёт из .env.example, запрашивает секреты, ставит права 600;
#   3. ufw: оставляет только 22, 80, 443;
#   4. fail2ban: sshd + nginx-limit-req, logrotate для журналов nginx;
#   5. каталог бэкапов и файл парольной фразы для gpg;
#   6. первый сертификат Let's Encrypt (standalone, пока nginx не слушает 80);
#   7. docker compose up -d --build;
#   8. таймеры systemd: ежедневный бэкап и продление сертификата;
#   9. deploy/verify.sh — автоматические проверки из чек-листа аудита.
#
# Секреты скрипт не печатает и в журнал не пишет.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root

# ---------- 0. Окружение ----------
. /etc/os-release
case "${ID:-}" in
    ubuntu|debian) ;;
    *) die "нужна Ubuntu 22.04/24.04 или Debian 12, а найдено: ${PRETTY_NAME:-неизвестно}" ;;
esac

REAL_USER="${SUDO_USER:-root}"
log "Проект: $ROOT_DIR"
log "ОС: ${PRETTY_NAME:-$ID}, пользователь $REAL_USER"

# ---------- 1. .env и секреты ----------
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    log "Создан .env из .env.example"
fi

# Запрос значения, если оно пустое. Третий аргумент — скрывать ввод.
# Файл .env не исполняется, значения читаются и пишутся как текст.
ask() {
    local key=$1 prompt=$2 hidden=${3:-} value again current
    current=$(env_get "$key")
    if [ -n "$current" ]; then
        log "$key уже заполнен — оставляю как есть"
        return 0
    fi
    if [ ! -t 0 ]; then
        warn "$key пуст. Заполните .env вручную и запустите скрипт снова."
        return 0
    fi
    while :; do
        if [ -n "$hidden" ]; then
            printf '%s (ввод не отображается): ' "$prompt" >&2
            read -rs value || true; printf '\n' >&2
        else
            printf '%s: ' "$prompt" >&2
            read -r value || true
        fi
        if [ -z "$value" ]; then
            warn "$key оставлен пустым"
            return 0
        fi
        if [ -n "$hidden" ]; then
            printf 'повторите ввод: ' >&2
            read -rs again || true; printf '\n' >&2
            if [ "$value" != "$again" ]; then
                warn "значения не совпали, попробуйте ещё раз"
                continue
            fi
        fi
        env_set "$key" "$value"
        log "$key записан в .env"
        return 0
    done
}

ask DOMAIN          "Домен сайта"
ask CERTBOT_EMAIL   "E-mail для Let's Encrypt"
ask ADMIN_PASSWORD  "Пароль администратора для входа в Telegram-ботов" secret
ask TELEGRAM_BOT_TOKEN  "Токен бота заявок (@BotFather)"                    secret
ask TELEGRAM_ADMIN_TOKEN "Токен бота админки (@BotFather)"                  secret

require_env DOMAIN CERTBOT_EMAIL
DOMAIN=$(env_get DOMAIN)
CERTBOT_EMAIL=$(env_get CERTBOT_EMAIL)
LOG_DIR=$(env_get LOG_DIR);        LOG_DIR=${LOG_DIR:-/var/log/greengarden/nginx}
BACKUP_DIR=$(env_get BACKUP_DIR);  BACKUP_DIR=${BACKUP_DIR:-/var/backups/greengarden}
KEY_FILE=$(env_get BACKUP_PASSPHRASE_FILE); KEY_FILE=${KEY_FILE:-/root/.greengarden-backup-key}
[ -n "$(env_get ADMIN_PASSWORD)" ] || die "ADMIN_PASSWORD пуст: вход в ботов будет закрыт для всех"
if [ -z "$(env_get TELEGRAM_BOT_TOKEN)" ]; then
    warn "TELEGRAM_BOT_TOKEN пуст: заявки будут копиться в базе и уйдят в чат,"
    warn "как только токен появится (их досылает retry_unpushed)."
fi

# ---------- 2. Пакеты и Docker ----------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gnupg gpg \
    openssl ufw fail2ban >/dev/null
log "Пакеты установлены (curl, gpg, openssl, ufw, fail2ban)"

if ! command -v docker >/dev/null 2>&1; then
    log "Устанавливаю Docker CE из download.docker.com"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$ID ${VERSION_CODENAME:-stable} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1
docker compose version >/dev/null 2>&1 || die "docker compose недоступен: поставьте docker-compose-plugin"
log "Docker: $(docker --version | cut -d' ' -f3 | tr -d ','), compose $(docker compose version --short)"

if [ "$REAL_USER" != root ] && ! id -nG "$REAL_USER" | grep -qw docker; then
    usermod -aG docker "$REAL_USER"
    log "$REAL_USER добавлен в группу docker (права действуют после повторного входа)"
fi

# ---------- 3. Firewall ----------
# Важная особенность связки ufw + Docker: опубликованные порты контейнеров
# попадают в iptables правилами самого Docker (цепочка DOCKER), минуя ufw.
# Поэтому защита здесь не правилами на 8000, а тем, что в docker-compose.yml
# опубликованы только 80 и 443 — порт приложения наружу не виден вовсе.
# DEFAULT_FORWARD_POLICY=ACCEPT обязателен: при DROP контейнеры теряют выход
# в интернет, и боты не смогут достучаться до api.telegram.org.
if command -v ufw >/dev/null 2>&1; then
    sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
    ufw allow OpenSSH >/dev/null
    ufw allow 80/tcp  >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw --force enable >/dev/null
    ufw reload >/dev/null 2>&1 || true
    log "ufw: открыты 22, 80, 443; FORWARD=ACCEPT"
fi

# ---------- 4. fail2ban и ротация журналов ----------
mkdir -p "$LOG_DIR"
if [ -d /etc/fail2ban ]; then
    sed "s|__LOG_DIR__|$LOG_DIR|g" deploy/fail2ban/greengarden.local \
        > /etc/fail2ban/jail.d/greengarden.local
    systemctl enable --now fail2ban >/dev/null 2>&1
    fail2ban-client reload >/dev/null 2>&1 || systemctl restart fail2ban
    log "fail2ban: включены джейлы sshd и nginx-limit-req"
fi
sed "s|__LOG_DIR__|$LOG_DIR|g" deploy/logrotate/greengarden > /etc/logrotate.d/greengarden
chmod 644 /etc/logrotate.d/greengarden
log "logrotate: журналы nginx в $LOG_DIR"

# ---------- 5. Бэкапы ----------
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
if [ ! -f "$KEY_FILE" ]; then
    ( umask 077; openssl rand -base64 32 > "$KEY_FILE" )
    chmod 600 "$KEY_FILE"
    log "Создан файл парольной фразы бэкапа: $KEY_FILE"
    warn "Скопируйте $KEY_FILE в надёжное место — без него архивы не расшифровать."
fi

# ---------- 6. Сертификат Let's Encrypt ----------
have_cert() {
    "${COMPOSE[@]}" --profile tools run --rm --no-deps -T certbot \
        sh -c "test -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem" >/dev/null 2>&1
}

# Проверка DNS: выпуск сертификата невозможен, пока домен не указывает сюда.
SITE_IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
DNS_IP=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')
if [ -n "$SITE_IP" ] && [ -n "$DNS_IP" ] && [ "$SITE_IP" != "$DNS_IP" ]; then
    warn "$DOMAIN указывает на $DNS_IP, а адрес сервера — $SITE_IP."
    warn "Сертификат не выпустится, пока A-запись не будет направлена сюда."
fi

if have_cert; then
    log "Сертификат для $DOMAIN уже есть"
else
    # Первый выпуск — в режиме standalone: certbot сам слушает 80, поэтому
    # nginx на это время должен быть остановлен. Продление позже идёт через
    # webroot и nginx не мешает (deploy/renew-cert.sh).
    "${COMPOSE[@]}" stop nginx >/dev/null 2>&1 || true
    sleep 2
    log "Выпускаю сертификат для $DOMAIN (порт 80 занят certbot)"
    "${COMPOSE[@]}" --profile tools run --rm --no-deps -T -p 80:80 certbot certonly \
        --standalone --non-interactive --agree-tos --keep-until-expiring \
        -m "$CERTBOT_EMAIL" -d "$DOMAIN" \
        || die "certbot не выпустил сертификат. Частые причины: A-запись $DOMAIN
       ещё не указывает на этот сервер ($DNS_IP вместо $SITE_IP), порт 80 закрыт
       у хостера, либо домен уже исчерпал лимит выпусков Let's Encrypt.
       Исправьте причину и запустите скрипт повторно — он идемпотентен."
fi

# ---------- 7. Сборка и запуск ----------
log "Собираю образы и поднимаю контейнеры"
"${COMPOSE[@]}" up -d --build

# ---------- 8. Таймеры systemd ----------
for unit in greengarden-backup.service greengarden-backup.timer \
            greengarden-certbot.service greengarden-certbot.timer; do
    sed "s|__PROJECT_DIR__|$ROOT_DIR|g" "deploy/systemd/$unit" > "/etc/systemd/system/$unit"
    chmod 644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable --now greengarden-backup.timer greengarden-certbot.timer >/dev/null 2>&1
log "Таймеры включены: $(systemctl list-timers --no-pager greengarden-\* 2>/dev/null | awk 'NR>1&&NF{print $NF}' | tr '\n' ' ')"

# ---------- 9. Проверки ----------
echo
if bash deploy/verify.sh; then
    log "Проверки пройдены"
else
    warn "Часть проверок не прошла — смотрите вывод выше и docker compose logs"
fi

cat <<EOF

Готово.

  Сайт:        https://$DOMAIN
  Контейнеры:  docker compose ps
  Журналы:     docker compose logs -f app      (заявки и события безопасности)
               docker compose logs -f nginx    (ошибки; доступ — в $LOG_DIR)
  Обновление:  sudo bash deploy/update.sh      (git pull + пересборка + проверки)
  Бэкап:       sudo bash deploy/backup.sh      (и ежедневно в 03:15 по таймеру)
  Сертификат:  продлевается сам в 04:30 и 16:30 (systemctl list-timers)

Что осталось сделать руками (см. DEPLOYMENT.md, раздел «После первого запуска»):
  1. В Telegram: /start в боте заявок и ввести ADMIN_PASSWORD.
  2. Отправить тестовую заявку с сайта и убедиться, что она пришла в чат.
  3. Настроить выгрузку бэкапов $BACKUP_DIR во внешнее хранилище (S3/Backblaze).
EOF
