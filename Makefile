# Короткие команды для сервера и локальной разработки.
# Всё это делают скрипты из deploy/ — Makefile лишь сокращает набор текста.
#
#   make help          список команд
#   make setup         первичная настройка сервера (от root)
#   make verify        проверки сайта после деплоя
#
# На сервере compose-файл читает переменные из .env (см. .env.example).

COMPOSE ?= docker compose
PROJECT ?= greengarden

.DEFAULT_GOAL := help
.PHONY: help dev lint setup update verify backup restore renew \
        build up down restart ps logs logs-app logs-nginx shell data clean-images

help: ## Показать этот список
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------- Локальная разработка (Docker не нужен) ----------

dev: ## Запустить сайт локально: python3 backend/app.py (порт 8000)
	python3 backend/app.py

lint: ## Проверить синтаксис Python-файлов
	python3 -m py_compile backend/app.py tools/*.py

# ---------- Сервер ----------

setup: ## Первичная настройка сервера: sudo make setup
	sudo bash deploy/setup.sh

update: ## Обновить сайт: git pull + пересборка + проверки
	bash deploy/update.sh

verify: ## Прогнать автоматические проверки сайта
	bash deploy/verify.sh

backup: ## Сделать шифрованную копию заявок
	sudo bash deploy/backup.sh

restore: ## Восстановить из архива: make restore ARCHIVE=/var/backups/greengarden/имя.tar.gz.gpg
	@[ -n "$(ARCHIVE)" ] || { echo "укажите ARCHIVE=<путь к .tar.gz.gpg>"; exit 1; }
	sudo bash deploy/restore.sh "$(ARCHIVE)"

renew: ## Проверить срок сертификата и продлить, если пора
	sudo bash deploy/renew-cert.sh

# ---------- Повседневное ----------

build: ## Пересобрать образы
	$(COMPOSE) build

up: ## Поднять контейнеры (пересоздаёт изменившиеся)
	$(COMPOSE) up -d

down: ## Остановить и убрать контейнеры (тома с данными остаются)
	$(COMPOSE) down

restart: ## Перезапустить контейнеры
	$(COMPOSE) restart

ps: ## Статус контейнеров и их здоровье
	$(COMPOSE) ps

logs: ## Журналы всех сервисов
	$(COMPOSE) logs --tail=100 -f

logs-app: ## Журнал приложения: заявки и события безопасности
	$(COMPOSE) logs --tail=200 -f app

logs-nginx: ## Журнал nginx (ошибки; журнал доступа — в LOG_DIR на хосте)
	$(COMPOSE) logs --tail=200 -f nginx

shell: ## Оболочка внутри контейнера приложения
	$(COMPOSE) exec app sh

data: ## Показать содержимое каталога данных и число заявок
	$(COMPOSE) exec app sh -c 'ls -la /data; echo; python -c "import json;print(len(json.load(open(\"/data/leads.json\"))),\"заявок\")"'

clean-images: ## Удалить образы прежних сборок (данные не трогает)
	docker image prune -f
