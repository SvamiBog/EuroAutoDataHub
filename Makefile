# EuroAutoDataHub Makefile

DC = docker compose

# Docker Compose команды
dc-up:
	$(DC) up -d

dc-down:
	$(DC) down

dc-build:
	$(DC) build

dc-restart:
	@echo "--- Перезапуск всех сервисов ---"
	$(DC) down
	$(DC) up -d

dc-rebuild:
	@echo "--- Остановка, удаление, пересборка и запуск всех сервисов ---"
	$(DC) down
	$(DC) build
	$(DC) up -d

dc-rebuild-service:
	@echo "--- Пересборка конкретного сервиса (использование: make dc-rebuild-service SERVICE=имя_сервиса) ---"
	$(DC) stop $(SERVICE)
	$(DC) build --no-cache $(SERVICE)
	$(DC) up -d $(SERVICE)

dc-fresh:
	@echo "--- Полная очистка и пересборка (удаляет volumes и images) ---"
	$(DC) down -v --remove-orphans
	$(DC) build --no-cache --pull
	$(DC) up -d

# Scrapy команды
SCRAPY_DIR = services/scrapy_spiders/car_scrapers
# Ограничить обход марками: make run-oto MAKES=audi,bmw
MAKES ?=
MAKES_ARG = $(if $(MAKES),-a makes=$(MAKES))

run-oto-local:
	@echo "--- Перехожу в $(SCRAPY_DIR) и запускаю Scrapy Local ---"
	@cd $(SCRAPY_DIR) && KAFKA_BOOTSTRAP_SERVERS=$${KAFKA_BOOTSTRAP_SERVERS:-localhost:9094} uv run scrapy crawl otomoto $(MAKES_ARG)

run-oto:
	@echo "--- Запуск Scrapy через главный docker-compose ---"
	$(DC) run --rm scrapy_runner scrapy crawl otomoto $(MAKES_ARG)

# Логи сервисов
logs-processor:
	$(DC) logs -f data_processor

logs-updater:
	$(DC) logs -f status_updater

logs-api:
	$(DC) logs -f api_service

logs-kafka:
	$(DC) logs -f kafka_broker

logs-db:
	$(DC) logs -f db_postgres

# API команды
api-dev:
	cd services/api_service && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

api-test:
	curl -X GET "http://localhost:8000/health" -H "accept: application/json"

api-docs:
	@echo "API Documentation available at: http://localhost:8000/docs"
	@echo "Redoc Documentation available at: http://localhost:8000/redoc"

# Миграции базы данных
db-upgrade:
	$(DC) run --rm api_migrations alembic upgrade head

db-revision:
	$(DC) run --rm api_migrations alembic revision --autogenerate -m "$(msg)"

# Заполнить справочник марок (car_make) из справочника паука
db-seed-makes:
	PYTHONPATH=. uv run python scripts/populate_car_makes.py

# Проверка статуса сервисов
status:
	@echo "--- Статус всех сервисов ---"
	$(DC) ps

# Тесты: у сервисов одинаковое имя пакета `app`, поэтому каждый сервис тестируется отдельным процессом
define run_tests
	cd $(SCRAPY_DIR) && uv run pytest car_scrapers/tests $(1)
	cd services/data_processor && uv run pytest tests $(1)
	cd services/api_service && uv run pytest tests $(1)
endef

.PHONY: test test-warnings test-strict test-coverage test-quiet test-verbose lint
test:
	@echo "--- 🚀 Запуск всех тестов через pytest ---"
	$(call run_tests,-v)

test-warnings:
	@echo "--- 🚨 Запуск тестов с показом предупреждений ---"
	$(call run_tests,-v -s --tb=short)

test-strict:
	@echo "--- 🚫 Запуск тестов с ошибками на предупреждения ---"
	$(call run_tests,-v -W error::DeprecationWarning)

test-coverage:
	@echo "--- 📊 Запуск тестов с покрытием ---"
	$(call run_tests,-v --cov=. --cov-report=term-missing --cov-report=html)

test-quiet:
	@echo "--- 🤫 Запуск тестов без предупреждений ---"
	$(call run_tests,-q --disable-warnings)

test-verbose:
	@echo "--- 📝 Подробный запуск тестов ---"
	$(call run_tests,-vv -s --tb=long)

# Минимальный линт: синтаксические ошибки и неопределенные имена
lint:
	uv run ruff check --select E9,F63,F7,F82 services scripts


# Помощь
help:
	@echo "Доступные команды:"
	@echo "  dc-up              - Запуск всех сервисов"
	@echo "  dc-down            - Остановка всех сервисов"
	@echo "  dc-build           - Сборка всех сервисов"
	@echo "  dc-restart         - Перезапуск всех сервисов"
	@echo "  dc-rebuild         - Остановка, сборка и запуск"
	@echo "  dc-rebuild-service - Пересборка одного сервиса (SERVICE=имя)"
	@echo "  dc-fresh           - Полная очистка и пересборка (удаляет volumes)"
	@echo "  status             - Показать статус всех сервисов"
	@echo ""
	@echo "Парсинг:"
	@echo "  run-oto            - Запуск парсера Otomoto в Docker (MAKES=audi,bmw — только эти марки)"
	@echo "  run-oto-local      - Запуск парсера Otomoto локально (Kafka на localhost:9094)"
	@echo ""
	@echo "Логи:"
	@echo "  logs-processor | logs-updater | logs-api | logs-kafka | logs-db"
	@echo ""
	@echo "API команды:"
	@echo "  api-dev            - Запуск API в режиме разработки"
	@echo "  api-test           - Проверка работы API"
	@echo "  api-docs           - Информация о документации API"
	@echo ""
	@echo "База данных:"
	@echo "  db-upgrade         - Применение миграций"
	@echo "  db-revision        - Создание новой миграции (msg=описание)"
	@echo "  db-seed-makes      - Заполнить справочник марок"
	@echo ""
	@echo "Тестирование:"
	@echo "  test               - Запуск всех тестов"
	@echo "  test-warnings      - Запуск тестов с показом предупреждений"
	@echo "  test-quiet         - Запуск тестов без предупреждений"
	@echo "  test-strict        - Запуск тестов с ошибками на предупреждения"
	@echo "  test-verbose       - Подробный запуск тестов"
	@echo "  test-coverage      - Запуск тестов с покрытием кода"
	@echo "  lint               - Минимальный линт (синтаксис, неопределенные имена)"
