#!/usr/bin/env bash
# Проверки развёрнутого сайта — автоматическая часть чек-листа из раздела 7
# SECURITY-AUDIT.md. Запускается после setup.sh и после каждого update.sh.
#
#   bash deploy/verify.sh
#
# Локальный прогон без DNS и настоящего сертификата (стенд на 127.0.0.1):
#   BASE_URL=https://green-courtyard.space:8443 BASE_HTTP=http://127.0.0.1:8080 \
#   CURL_OPTS='--resolve green-courtyard.space:8443:127.0.0.1' bash deploy/verify.sh
#
# Выходной код 0 — все проверки прошли.

set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require_env DOMAIN
DOMAIN=$(env_get DOMAIN)

BASE="${BASE_URL:-https://$DOMAIN}"
BASE_HTTP="${BASE_HTTP:-http://$DOMAIN}"
# Браузерный User-Agent обязателен: python-urllib и curl/ приложение отклоняет
# с кодом 403 (защита F-12), и проверки выглядели бы как поломка сайта.
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'

CURL=(curl -sS -k --max-time 15 -A "$UA")
# shellcheck disable=SC2206  # намеренное разбиение строки параметров на слова
[ -n "${CURL_OPTS:-}" ] && CURL+=($CURL_OPTS)

PASS=0
FAIL=0
SKIP=0
SEEN_429=0

ok()   { PASS=$((PASS + 1)); printf '  \033[32m  ok\033[0m  %-44s %s\n' "$1" "${2:-}"; }
bad()  {
    FAIL=$((FAIL + 1))
    printf '  \033[31m FAIL\033[0m  %-44s ожидали «%s», получили «%s»\n' "$1" "$2" "$3"
    # 429 от nginx означает, что проверки сами уперлись в limit_req. Это не
    # поломка сайта: zone=per_ip считает 30 запросов в минуту на адрес.
    case "$3" in *429*) SEEN_429=1 ;; esac
}
skip() { SKIP=$((SKIP + 1)); printf '  \033[33m skip\033[0m  %-44s %s\n' "$1" "${2:-}"; }
check() { [ "$2" = "$3" ] && ok "$1" "$3" || bad "$1" "$2" "$3"; }

code() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@" 2>/dev/null; }
body() { "${CURL[@]}" "$@" 2>/dev/null; }
headers() { local url=$1; shift; "${CURL[@]}" -D - -o /dev/null "$url" "$@" 2>/dev/null | tr -d '\r'; }

echo "Проверяем $BASE"

# ---------- 1. Маршруты ----------
echo
log "Страницы и статика — ожидаем 200"
for p in / /catalog /catalog/abrikos-krasnoschekiy /collections/golubika \
         /guides /guides/kak-vybrat-sazhenets /services /additional-services \
         /contacts /delivery /admin /offer /privacy-policy /robots.txt \
         /sitemap.xml /css/style.css /js/main.js /img/logo.png; do
    check "GET $p" "200" "$(code "$BASE$p")"
done

log "Старые адреса .html — ожидаем 301"
for p in /index.html /catalog.html /catalog/abrikos-krasnoschekiy.html \
         /services.html /contacts.html; do
    check "GET $p" "301" "$(code "$BASE$p")"
done

log "Служебные пути — ожидаем 404 (а не 403: ответ не должен подтверждать существование)"
for p in /backend/app.py /backend/leads.json /backend/chats.json /backend/bot_token.txt \
         /tools/build_catalog.py /deploy/setup.sh /.gitignore /.env /img/ \
         "/img/%2e%2e/backend/app.py" "/css/%2e%2e/backend/bot_token.txt"; do
    check "GET $p" "404" "$(code "$BASE$p")"
done

# ---------- 2. Заголовки ----------
echo
log "Заголовки безопасности на главной"
H=$(headers "$BASE/")
check "X-Frame-Options"          "DENY"           "$(printf '%s\n' "$H" | awk 'tolower($1)=="x-frame-options:"{print $2}')"
check "X-Content-Type-Options"   "nosniff"        "$(printf '%s\n' "$H" | awk 'tolower($1)=="x-content-type-options:"{print $2}')"
check "Referrer-Policy"          "strict-origin"  "$(printf '%s\n' "$H" | awk 'tolower($1)=="referrer-policy:"{print $2}')"
check "Server (без версии)"      "nginx"          "$(printf '%s\n' "$H" | awk 'tolower($1)=="server:"{print $2}')"
check "HSTS присутствует"         "1"              "$(printf '%s\n' "$H" | grep -ci '^strict-transport-security:')"
check "CSP ровно один (без дублей)" "1"           "$(printf '%s\n' "$H" | grep -ci '^content-security-policy:')"
check "HTML не кешируется"       "no-cache"       "$(printf '%s\n' "$H" | awk 'tolower($1)=="cache-control:"{print $2}')"
check "CSP содержит frame-ancestors" "1"          "$(printf '%s\n' "$H" | grep -c "frame-ancestors 'none'")"

HA=$(headers "$BASE/css/style.css")
check "Cache-Control статики один" "1"            "$(printf '%s\n' "$HA" | grep -ci '^cache-control:')"

# ---------- 3. Редирект и отсечение чужих Host ----------
echo
log "HTTP -> HTTPS"
# Host подставляется явно: при проверке локального стенда по 127.0.0.1 запрос
# без него попал бы в catch-all-сервер nginx (обрыв соединения), а не в
# доменный, и рабочий редирект выглядел бы сломанным.
check "GET http:// — редирект 301" "301" "$(code -H "Host: $DOMAIN" "$BASE_HTTP/")"
LOC=$(headers "$BASE_HTTP/" -H "Host: $DOMAIN" | awk 'tolower($1)=="location:"{print $2}')
case "$LOC" in
    "https://$DOMAIN"/*) ok "Location ведёт на https://$DOMAIN" "$LOC" ;;
    *)                    bad "Location" "https://$DOMAIN/..." "${LOC:-пусто}" ;;
esac
check "чужой Host по HTTP (ожидаем обрыв соединения)" "000" \
    "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 -A "$UA" \
       -H 'Host: scanner.invalid' "$BASE_HTTP/" 2>/dev/null)"

# ---------- 4. API ----------
echo
log "POST /api/lead — заявки не должны создаваться проверками"
check "без согласия (152-ФЗ)" "400" "$(code -X POST -H 'Content-Type: application/json' \
    -d '{"name":"Проверка","phone":"+7 (900) 000-00-00","source":"cta","elapsed_ms":5000,"consent":false}' \
    "$BASE/api/lead")"
check "honeypot — тихий отказ" '{"ok": true, "id": 0}' "$(body -X POST -H 'Content-Type: application/json' \
    -d '{"name":"Проверка","phone":"+7 (900) 000-00-00","website":"x","source":"cta","elapsed_ms":5000,"consent":true}' \
    "$BASE/api/lead")"
check "мгновенная отправка — тихий отказ" '{"ok": true, "id": 0}' "$(body -X POST -H 'Content-Type: application/json' \
    -d '{"name":"Проверка","phone":"+7 (900) 000-00-00","source":"cta","elapsed_ms":10,"consent":true}' \
    "$BASE/api/lead")"

# ---------- 5. Контейнеры ----------
echo
log "Состояние контейнеров"
if docker info >/dev/null 2>&1; then
    for s in app nginx; do
        cid=$("${COMPOSE[@]}" ps -q "$s" 2>/dev/null || true)
        if [ -z "$cid" ]; then bad "$s" "работает" "контейнер не найден"; continue; fi
        st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}/{{.State.Status}}' "$cid")
        check "$s" "healthy/running" "$st"
    done
    check "порт приложения не опубликован на хосте" "8000/tcp" \
        "$("${COMPOSE[@]}" ps --format '{{.Ports}}' app 2>/dev/null)"
    check "каталог данных доступен на запись" "ok" \
        "$("${COMPOSE[@]}" exec -T app sh -c 'touch /data/.probe && rm -f /data/.probe && echo ok' 2>/dev/null)"
else
    skip "контейнеры" "docker недоступен на этой машине"
fi

# ---------- 6. Сертификат ----------
echo
log "Сертификат Let's Encrypt"
if [ "${BASE}" = "https://$DOMAIN" ]; then
    CERT=$(printf '' | openssl s_client -connect "$DOMAIN:443" -servername "$DOMAIN" 2>/dev/null)
    END=$(printf '%s' "$CERT" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    # -checkend 1728000 — сертификат действует дольше 20 дней. Продлением
    # занимается таймер greengarden-certbot.timer, порог взят с запасом,
    # чтобы проверка предупреждала о проблеме до истечения.
    if [ -n "$END" ] && printf '%s' "$CERT" | openssl x509 -noout -checkend 1728000 >/dev/null 2>&1; then
        ok "сертификат действует" "до $END"
    elif [ -n "$END" ]; then
        bad "срок сертификата" "действует дольше 20 дней" "истекает $END"
    else
        bad "сертификат" "прочитан" "не получен"
    fi
else
    skip "сертификат" "проверяется не по доменному имени ($BASE)"
fi

# ---------- Итог ----------
echo
printf 'Итого: \033[32m%d ok\033[0m, \033[31m%d провалов\033[0m, %d пропущено\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -ne 0 ]; then
    if [ "$SEEN_429" -eq 1 ]; then
        echo "В ответах встретился 429: сами проверки исчерпали limit_req nginx"
        echo "(30 запросов в минуту на адрес). Подождите минуту и запустите снова —"
        echo "повторный запуск подряд — штатная причина таких провалов, а не отказ сайта."
    fi
    echo "Проверки не пройдены. Журналы: docker compose logs --tail=100 app nginx"
    exit 1
fi
echo "Сайт отвечает правильно. Осталось проверить руками формы и доставку заявок в Telegram."
