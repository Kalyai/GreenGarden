#!/usr/bin/env bash
# Продление сертификата Let's Encrypt и перезагрузка nginx.
#
#   sudo bash deploy/renew-cert.sh          (обычно запускается таймером)
#
# В отличие от первого выпуска (deploy/setup.sh, режим standalone) продление
# идёт через webroot: nginx остаётся в работе и сам отдаёт файлы проверок из
# каталога, примонтированного в контейнер certbot. Сайт не простаивает.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_root
require_env DOMAIN

DOMAIN=$(env_get DOMAIN)

log "Проверяю срок сертификата для $DOMAIN"
# renew сам решает, продлевать ли: certbot трогает только сертификаты,
# до истечения которых меньше 30 дней.
if "${COMPOSE[@]}" --profile tools run --rm --no-deps -T certbot \
        renew --webroot -w /var/www/certbot --quiet; then
    log "certbot отработал без ошибок"
else
    die "certbot завершился с ошибкой — проверьте: docker compose logs certbot"
fi

# Перезагружаем nginx в любом случае: это дёшево, не рвёт соединения
# (graceful reload) и гарантирует, что воркеры подхватят новый сертификат.
if "${COMPOSE[@]}" ps -q nginx >/dev/null 2>&1 && [ -n "$("${COMPOSE[@]}" ps -q nginx)" ]; then
    "${COMPOSE[@]}" exec -T nginx nginx -s reload
    log "nginx перезагружен"
else
    warn "nginx не запущен — перезапуск пропущен"
fi

EXPIRES=$("${COMPOSE[@]}" --profile tools run --rm --no-deps -T certbot \
    certificates 2>/dev/null | grep -E 'Expiry Date' | head -1 | sed 's/.*Expiry Date[[:space:]]*:[[:space:]]*//')
log "Сертификат: ${EXPIRES:-срок не определён}"
