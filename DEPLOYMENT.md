# Развёртывание сайта «Зелёный дворик»

Инструкция для сервера Ubuntu 22.04/24.04 или Debian 12. Всё, что нужно сделать
на сервере, — склонировать репозиторий, заполнить `.env` и запустить один скрипт.

Требования безопасности соответствуют `SECURITY-AUDIT.md`: приложение не
слушает внешний интерфейс, секреты и персональные данные лежат вне web-root,
TLS терминируется на nginx, заявки не покидают сервер в открытом виде.

---

## 1. Что получается в итоге

```
посетитель ──HTTPS──> nginx :443 ──┬── статика (вшита в образ nginx)
                                   └── /api/ ──> app :8000 ──> api.telegram.org
                                     (только внутри сети Docker, наружу не виден)

Let's Encrypt ──HTTP :80──> nginx ──> том certbot-www (проверки домена)
```

| Контейнер | Что делает | Что опубликовано наружу |
|---|---|---|
| `nginx` | TLS, раздача страниц и изображений, лимиты запросов, проксирование `/api/` | `80`, `443` |
| `app` | `backend/app.py`: приём заявок и два Telegram-бота (long polling) | ничего |
| `certbot` | выпуск и продление сертификата; запускается только по требованию | ничего |

| Том | Содержимое |
|---|---|
| `greengarden_data` | `leads.json` (заявки, ПДн), `chats.json` (состояния чатов). Права `0700`, файлы `0600`, владелец — пользователь сервиса |
| `greengarden_certbot-etc` | сертификаты и ключи Let's Encrypt |
| `greengarden_certbot-www` | файлы проверок ACME |

Порт `8000` намеренно не публикуется: добраться до приложения можно только
через nginx, который перезаписывает `X-Forwarded-For` (подменить IP клиента
нельзя) и ограничивает частоту запросов.

---

## 2. Что понадобится до начала

- Сервер: 1 vCPU, 1 ГБ ОЗУ, 10 ГБ диска, Ubuntu 22.04/24.04 или Debian 12.
- Домен `greengarden.run.place` с **A-записью на IP сервера**.
- Доступ по SSH и права `sudo`.
- Токены двух ботов от @BotFather (бот заявок и бот админки).
- Docker ставить вручную не нужно — `deploy/setup.sh` поставит сам.

Проверка DNS перед началом (иначе сертификат не выпустится):

```bash
getent hosts greengarden.run.place      # должен вернуть IP этого сервера
curl -s https://api.ipify.org           # фактический внешний IP сервера
```

---

## 3. Шаг 1. Код на сервере

```bash
sudo mkdir -p /opt/greengarden && sudo chown "$USER" /opt/greengarden
git clone <URL вашего репозитория> /opt/greengarden
cd /opt/greengarden
```

Путь не принципиален: скрипты сами определяют каталог репозитория. `/opt`
удобен тем, что не принадлежит домашнему каталогу и переживает смену пользователя.

---

## 4. Шаг 2. Настройки и секреты

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Обязательные значения:

| Переменная | Где взять |
|---|---|
| `DOMAIN` | уже заполнен: `greengarden.run.place` |
| `CERTBOT_EMAIL` | ваша почта для уведомлений Let's Encrypt |
| `ADMIN_PASSWORD` | придумайте: пароль входа в оба Telegram-бота |
| `TELEGRAM_BOT_TOKEN` | @BotFather → токен бота заявок |
| `TELEGRAM_ADMIN_TOKEN` | @BotFather → токен бота админки |

Правила заполнения `.env`:

- значения **без кавычек и без символа `#`** (всё после `#` считается комментарием);
- файл не попадает в git (`.gitignore`) и в образы (`.dockerignore`);
- `ADMIN_PASSWORD` — единственный секрет, который нужно помнить наизусть:
  его вводят в Telegram при первом `/start`.

`deploy/setup.sh` сам спросит всё, что осталось пустым (ввод пароля и токенов
не отображается на экране), — так что можно просто запустить его и отвечать.

> Если репозиторий когда-либо публиковался с токенами в истории — отзовите их
> через @BotFather (`/revoke`) и выпустите новые. Это пункт чек-листа аудита.

---

## 5. Шаг 3. Запуск

```bash
sudo bash deploy/setup.sh
```

Скрипт идемпотентен: повторный запуск не ломает работающий сайт. Он выполняет:

1. установка Docker CE и compose-плагина из официального репозитория;
2. заполнение пустых значений в `.env`, права `600`;
3. `ufw`: открыты только 22, 80, 443 (`DEFAULT_FORWARD_POLICY=ACCEPT`, иначе
   контейнеры теряют выход в интернет и боты не достучатся до Telegram);
4. `fail2ban`: джейлы `sshd` и `nginx-limit-req`, `logrotate` для журналов nginx;
5. каталог бэкапов `0700` и файл парольной фразы `0600` для gpg;
6. первый сертификат Let's Encrypt (режим `standalone`, пока nginx не слушает 80);
7. `docker compose up -d --build`;
8. таймеры systemd: бэкап в 03:15 и проверка сертификата в 04:30/16:30;
9. `deploy/verify.sh` — автоматические проверки сайта.

В конце скрипт печатает шпаргалку по командам.

---

## 6. Шаг 4. После первого запуска

1. **Подключите ботов.** В Telegram откройте бота заявок → `/start` → введите
   `ADMIN_PASSWORD`. Повторите для бота админки. Без этого заявки копятся в
   базе и не приходят в чат (они будут досланы автоматически после активации).
2. **Отправьте тестовую заявку с сайта** (форма «Заказать звонок» и заказ из
   корзины) и убедитесь, что карточка пришла в Telegram с составом и количеством.
3. **Прогоните проверки:**
   ```bash
   bash deploy/verify.sh
   ```
   Ожидается `0 провалов`. Скрипт проверяет маршруты, закрытые пути, заголовки
   безопасности, редирект на HTTPS, приём заявок, состояние контейнеров и срок
   сертификата. Заявок он не создаёт.
4. **Настройте внешнюю копию бэкапов.** Локальный архив исчезнет вместе с
   сервером. Пример выгрузки (rclone в S3/Backblaze/Яндекс.Диск):
   ```bash
   rclone copy /var/backups/greengarden remote:greengarden-backups
   ```
   Добавьте это в конец `/etc/cron.daily/` или в `deploy/backup.sh`.
5. **Проверьте восстановление** — бэкап, который ни разу не восстанавливали,
   бэкапом не является:
   ```bash
   sudo bash deploy/restore.sh /var/backups/greengarden/<имя>.tar.gz.gpg
   ```

---

## 7. Повседневные операции

| Задача | Команда |
|---|---|
| Статус контейнеров | `docker compose ps` |
| Журнал заявок и событий безопасности | `docker compose logs -f app` |
| Журнал ошибок nginx | `docker compose logs -f nginx` |
| Журнал доступа nginx | `tail -f /var/log/greengarden/nginx/access.log` |
| Обновить сайт | `bash deploy/update.sh` |
| Перезапустить | `docker compose restart app` |
| Сделать бэкап | `sudo bash deploy/backup.sh` |
| Восстановить | `sudo bash deploy/restore.sh <архив>` |
| Продлить сертификат вручную | `sudo bash deploy/renew-cert.sh` |
| Проверки сайта | `bash deploy/verify.sh` |
| Зайти в контейнер | `docker compose exec app sh` |
| Число заявок в базе | `make data` |

Те же команды короче через `make`: `make logs-app`, `make backup`, `make verify`.

---

## 8. Обновление сайта

```bash
cd /opt/greengarden
git pull --ff-only            # или положитесь на update.sh
bash deploy/update.sh
```

`update.sh` делает `git pull`, пересборку обоих образов, пересоздание
контейнеров, `docker image prune` и прогоняет `verify.sh`.

- **HTML, `js/main.js` и `backend/app.py` выкатываются одновременно** — это
  требование чек-листа аудита (F-09 меняет контракт формы и `/api/lead`). Здесь
  оно гарантировано конструкцией: статика и код приложения собираются из одного
  рабочего дерева одним `Dockerfile`.
- Простой — 1–3 секунды на пересоздание контейнеров. Аудит рекомендует
  выкатывать обновления в часы низкого трафика.
- Данные не затрагиваются: `leads.json` и `chats.json` живут в именованном томе.
- Откат: `git checkout <прежний коммит> && bash deploy/update.sh --no-pull`.
- После выкладки следите в журнале за событием `legacy_client`: его всплеск
  означает, что часть посетителей всё ещё работает со старой версией `main.js`.

---

## 9. Журналы: что смотреть

События безопасности пишет приложение (`docker compose logs app`):

| Событие | О чём говорит |
|---|---|
| `lead_anomaly` | всплеск заявок (порог `LEAD_ALERT_THRESHOLD`) — спам или атака |
| `bot_login_blocked` | пять неверных паролей — кто-то подбирает доступ к боту |
| `path_denied` | попытка прочитать служебные файлы |
| `rate_limited` | клиент упёрся в лимит заявок |
| `honeypot_hit`, `too_fast_submit` | отсеянные автоматические отправки |
| `legacy_client` | клиент без `elapsed_ms` — старая страница или бот |
| `data_dir_unwritable` | том данных не пишется: заявки будут теряться, чинить сразу |
| `admin_password_missing` | контейнер запущен без `ADMIN_PASSWORD` |

Быстрый осмотр за сутки:

```bash
docker compose logs --since 24h app | grep -E 'security|lead_anomaly|blocked' | tail -50
```

Журналы контейнеров ротируются самим Docker (`max-size=10m`, 3 файла — см.
`docker-compose.yml`), журналы nginx — `logrotate` (`/etc/logrotate.d/greengarden`).

---

## 10. Диагностика

| Симптом | Причина и действие |
|---|---|
| Сайт не открывается, `curl` молчит | `docker compose ps` — поднят ли nginx; `ufw status`; открыты ли 80/443 у хостера |
| Ошибка сертификата в браузере | `sudo bash deploy/renew-cert.sh`; `docker compose logs certbot`; A-запись домена |
| Формы «спасибо», а заявок в Telegram нет | бот не активирован (`/start` + пароль) или пустой `TELEGRAM_BOT_TOKEN`; `docker compose logs app \| grep bot` |
| Заявки есть в базе, но не приходят в чат | они досылаются автоматически (`retry_unpushed`) при следующем опросе Telegram; проверьте доступ контейнера в интернет |
| `502`/`504` на `/api/` | `docker compose ps app`, `docker compose logs app` — приложение не поднялось или не прошло `ensure_data_dir` |
| `429` у реальных посетителей | лимиты: `LEAD_LIMIT_PER_MIN` в `.env` и `zone=api`/`zone=per_ip` в шаблоне nginx. Для офиса за NAT мягче меняют оба места |
| Контейнер `app` в цикле перезапуска | `docker compose logs app` — чаще всего нет прав на том или не задан `ADMIN_PASSWORD` |
| Бот отвечает «Вход недоступен» | `ADMIN_PASSWORD` пуст в `.env`: заполните и `docker compose up -d` |
| Подсеть `172.28.0.0/24` занята | смените `APP_SUBNET` в `.env` и пересоздайте сеть: `docker compose down && docker compose up -d` |
| После пересборки nginx не видит app | не должно случаться: в конфиге `resolver 127.0.0.11` и переменная в `proxy_pass`. Если всё же — `docker compose restart nginx` |

---

## 11. Безопасность: что уже сделано и что осталось

Автоматически при развёртывании:

- приложение слушает `0.0.0.0` только внутри сети Docker, порт не опубликован;
- `TRUSTED_PROXIES` = подсеть compose, nginx **перезаписывает** `X-Forwarded-For`;
- контейнеры: `read_only`, `cap_drop: ALL`, `no-new-privileges`, непривилегированный
  пользователь `gd` (uid 10001); у nginx добавлены только `NET_BIND_SERVICE`,
  `SETUID`, `SETGID`, `CHOWN`;
- данные: том `0700`, файлы `0600` (`umask 0077`), вне web-root;
- `.env` и токены не попадают ни в git, ни в контекст сборки;
- TLS 1.2/1.3, HSTS, CSP и остальные заголовки — и на статике, и на API;
- `ssl_reject_handshake` и `444` для запросов по IP и с чужим `Host`;
- `ufw` (22/80/443), `fail2ban` (sshd + nginx-limit-req), ротация журналов;
- бэкапы шифруются gpg (AES256), открытый снимок уничтожается `shred`.

Остаётся сделать руками:

- [ ] внешняя копия бэкапов (S3/Backblaze) и **проверенное** восстановление;
- [ ] автообновления ОС: `apt install unattended-upgrades && dpkg-reconfigure -plow unattended-upgrades`;
- [ ] мониторинг с алертами на `lead_anomaly`, `bot_login_blocked`, всплеск `path_denied`;
- [ ] ротация токенов, если репозиторий публиковался;
- [ ] SSH только по ключам: `PasswordAuthentication no` в `/etc/ssh/sshd_config`;
- [ ] `docker compose logs app | grep data_dir_unwritable` — пусто.

---

## 12. Локальная разработка (Docker не нужен)

```bash
python3 backend/app.py          # http://127.0.0.1:8000
```

Приложение слушает `127.0.0.1`, состояние пишет в `backend/leads.json` и
`backend/chats.json`, токены берёт из `backend/bot_token.txt` и
`backend/admin_token.txt`. Это прежний способ запуска — он не сломан
добавлением `DATA_DIR` и `SITE_HOST`.

Проверить сборку образов локально:

```bash
docker compose config                       # валидность compose-файла
docker build --target app   -t gd/app .
docker build --target nginx -t gd/nginx .
```

---

## 13. Карта файлов развёртывания

```
Dockerfile                         три стадии: static (публичные файлы), app, nginx
.dockerignore                      секреты и ПДн не попадают в контекст сборки
docker-compose.yml                 app + nginx + certbot, тома, сеть, лимиты журнала
.env.example                       шаблон настроек (копируется в .env)
Makefile                           короткие команды (make help)
deploy/
  setup.sh                         первичная настройка сервера — ЗАПУСКАТЬ ПЕРВЫМ
  update.sh                        git pull + пересборка + проверки
  verify.sh                        автоматические проверки сайта (чек-лист аудита)
  backup.sh                        шифрованный снимок тома данных
  restore.sh                       восстановление из архива
  renew-cert.sh                    продление сертификата + reload nginx
  common.sh                        общий пролог скриптов (чтение .env, лог)
  nginx/greengarden.conf.template  конфиг nginx (домен подставляется envsubst)
  nginx/snippets/security-headers.conf
  systemd/greengarden-backup.{service,timer}
  systemd/greengarden-certbot.{service,timer}
  fail2ban/greengarden.local       джейлы sshd и nginx-limit-req
  logrotate/greengarden            ротация журналов nginx
.github/workflows/ci.yml           сборка образов и дымовые тесты при push/PR
```
