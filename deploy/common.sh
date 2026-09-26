# Общий пролог скриптов deploy/*.sh. Не запускать самостоятельно.
#
# Подключается так:  source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# Даёт: каталог проекта, чтение .env без его выполнения, compose-обёртку и лог.

# Каталог репозитория: deploy/ -> на уровень вверх. Все пути в скриптах
# относительны от него, поэтому запускать их можно из любого места.
ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT_DIR"

# compose-вызов. Профиль tools подключает сервис certbot, который не стартует
# вместе с `up -d`. Файл compose-проекта один и лежит в корне репозитория.
COMPOSE=(docker compose)

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "скрипт нужно запускать от root: sudo bash $0"
}

# Значение переменной из .env. Файл намеренно НЕ исполняется через source:
# в нём лежат секреты, а выполнение чужого файла — это выполнение кода.
# Значения в .env пишите без кавычек и без символа «#».
env_get() {
    local line
    line=$(grep -E "^$1=" .env 2>/dev/null | tail -n 1 || true)
    [ -n "$line" ] || return 0
    printf '%s' "${line#*=}"
}

# Заменить или добавить KEY=VALUE в .env. Значение передаётся через окружение,
# а не через -v awk: иначе awk развернул бы управляющие последовательности
# в пароле (\n, \t) и молча испортил секрет.
env_set() {
    local tmp
    [ -f .env ] || cp .env.example .env
    tmp=$(mktemp)
    GD_KEY="$1" GD_VALUE="$2" awk '
        BEGIN { k = ENVIRON["GD_KEY"]; v = ENVIRON["GD_VALUE"]; done = 0 }
        index($0, k "=") == 1 { print k "=" v; done = 1; next }
        { print }
        END { if (!done) print k "=" v }
    ' .env > "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
    chmod 600 .env
}

# Прочитать .env и убедиться, что обязательные значения заданы.
require_env() {
    [ -f .env ] || die "нет файла .env — скопируйте: cp .env.example .env && chmod 600 .env"
    local key
    for key in "$@"; do
        [ -n "$(env_get "$key")" ] || die "в .env не заполнено: $key"
    done
}
