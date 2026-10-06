# syntax=docker/dockerfile:1
#
# «Зелёный дворик»: один контекст сборки — три стадии.
#
#   static — только публичные файлы сайта (источник для COPY, без ОС);
#   app    — процесс Python: каталог, админка, заявки и Telegram-бот;
#   nginx  — раздача статики и TLS-терминация.
#
# Оба образа собираются из одного коммита, поэтому HTML, js/main.js и
# backend/app.py всегда выкатываются вместе — это требование чек-листа аудита
# (F-09 меняет контракт между фронтендом и /api/lead).
#
# Зависимостей pip нет (см. requirements.txt), поэтому установка библиотек
# в образе отсутствует как класс: меньше слоёв и меньше поверхность атаки.
#
# Сборка:   docker compose build
# Локально: python3 backend/app.py   (Docker для разработки не обязателен)

# ---------- 1. Публичная статика ----------
# scratch: в стадии нечем выполнять код, она нужна только как источник COPY.
# backend/, tools/ и deploy/ сюда намеренно не попадают: в web-root не должно
# быть ни кода, ни данных, ни секретов (F-03, DENIED_DIRS, .dockerignore).
FROM scratch AS static

COPY *.html /static/
COPY catalog/ /static/catalog/
COPY collections/ /static/collections/
COPY guides/ /static/guides/
COPY sitemap.xml robots.txt /static/
COPY css/   /static/css/
COPY img/   /static/img/
COPY js/    /static/js/

# ---------- 2. Приложение ----------
# Версия Python — как в requirements.txt («проверено на Python 3.14»).
FROM python:3.14-slim AS app

# TZ важен для предметной области: заявки датируются локальным временем,
# и по нему же бот отбирает «заявки за сегодня». Без tzdata контейнер жил бы
# в UTC, и после 21:00 мск заявки попадали бы в следующий день.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    SITE_HOST=0.0.0.0 \
    SITE_PORT=8000 \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 gd \
    && useradd --system --uid 10001 --gid gd --shell /usr/sbin/nologin \
       --no-create-home --home-dir /nonexistent gd \
    && mkdir -p /data \
    && chown gd:gd /data \
    && chmod 700 /data

WORKDIR /app

# Статика в образе приложения нужна не для продакшена (её отдаёт nginx),
# а для HEALTHCHECK и для запуска без nginx — например, при диагностике.
COPY --from=static /static /app/
COPY backend/app.py backend/admin.py backend/catalog_store.py /app/backend/
COPY legacy-catalog.json /app/legacy-catalog.json
COPY tools/catalog_taxonomy.py tools/seo_content.py tools/seo_site.py tools/render_legacy_catalog.py /app/tools/

# Процесс слушает 0.0.0.0 только внутри своей сети контейнера: порт 8000
# не публикуется на хост (см. docker-compose.yml), снаружи его не достать.
EXPOSE 8000

# Проверка живости: процесс отвечает на GET / кодом 200. User-Agent обязан
# быть «браузерным» — python-urllib отклоняется защитой приложения (F-12).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/', headers={'User-Agent': 'Mozilla/5.0 (healthcheck)'}), timeout=4)"]

USER gd

# umask 0077: leads.json и chats.json создаются с правами 0600. Это файловый
# эквивалент отдельного пользователя БД с минимальными правами из ТЗ
# (SECURITY-AUDIT.md, раздел 4): каталог /data имеет 0700, а прочитать заявки
# может только пользователь сервиса.
CMD ["sh", "-c", "umask 0077 && exec python backend/app.py"]

# ---------- 3. nginx ----------
FROM nginx:1.28-alpine AS nginx

ENV TZ=Europe/Moscow

RUN apk add --no-cache tzdata

COPY --from=static /static /usr/share/nginx/html/

# Официальный образ сам рендерит *.template из /etc/nginx/templates в
# /etc/nginx/conf.d через envsubst: подставляются только переменные окружения
# контейнера (${DOMAIN}), а переменные nginx ($host, $uri) остаются нетронутыми.
COPY deploy/nginx/greengarden.conf.template /etc/nginx/templates/default.conf.template
COPY deploy/nginx/snippets/security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY deploy/nginx/snippets/proxy-app.conf /etc/nginx/snippets/proxy-app.conf

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1/nginx-health"]
