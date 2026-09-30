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

# Любой паук: make run-spider SPIDER=autoscout24 MAKES=bmw ARGS="-a countries=DE"
SPIDER ?= otomoto
ARGS ?=
run-spider:
	$(DC) run --rm scrapy_runner scrapy crawl $(SPIDER) $(MAKES_ARG) $(ARGS)

# Проверка паука на живом сайте без Kafka: пара страниц, сообщения в probe_<паук>.jsonl
# make probe SPIDER=autovit MAKES=dacia
probe:
	@cd $(SCRAPY_DIR) && rm -f probe_$(SPIDER).jsonl && uv run scrapy crawl $(SPIDER) $(MAKES_ARG) $(ARGS) \
		-s OUTPUT_FILE=probe_$(SPIDER).jsonl -s MAX_PAGES_PER_SHARD=$${PAGES:-2} -s LOG_FILE= -s PROGRESS_BAR=false
	@cd $(SCRAPY_DIR) && uv run python -m car_scrapers.probe probe_$(SPIDER).jsonl

# Логи сервисов
logs-ingestor:
	$(DC) logs -f ingestor

logs-scheduler:
	$(DC) logs -f scheduler

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

# Заполнить справочник марок (vehicle_make) из справочника паука
db-seed-makes:
	cd services/data_processor && uv run python -m app.seed_makes ../scrapy_spiders/car_scrapers/car_scrapers/data/otomoto_makes.json

# Проверка статуса сервисов
status:
	@echo "--- Статус всех сервисов ---"
	$(DC) ps

# Тесты: у сервисов одинаковое имя пакета `app`, поэтому каждый сервис тестируется отдельным процессом
define run_tests
	cd libs/eadh_common && uv run pytest tests $(1)
	cd bi/metabase && uv run pytest test_cards.py $(1)
	cd $(SCRAPY_DIR) && uv run pytest car_scrapers/tests $(1)
	cd services/data_processor && uv run pytest tests $(1)
	cd services/api_service && uv run pytest tests $(1)
endef

.PHONY: test test-warnings test-strict test-coverage test-quiet test-verbose lint e2e probe run-spider
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

# BI: Metabase (http://localhost:3000) и дашборды как код (bi/metabase)
bi-up:
	$(DC) --profile bi up -d metabase

# Metabase видит PostgreSQL по имени сервиса db_postgres внутри сети docker-compose
bi-provision:
	set -a; . ./.env; set +a; MB_DB_HOST=db_postgres MB_DB_PORT=5432 uv run python bi/metabase/provision.py

# Пересчёт витрины за период: make stats-backfill FROM=2026-09-01 TO=2026-09-30
stats-backfill:
	$(DC) exec ingestor python -m app.aggregates --from $(FROM) --to $(TO)

# Сквозная проверка паук -> Kafka -> ingestor -> PostgreSQL на запущенном стеке (make dc-up)
e2e:
	KAFKA_BOOTSTRAP_SERVERS=$${KAFKA_BOOTSTRAP_SERVERS:-localhost:9094} uv run python tests/e2e/run_e2e.py

# Минимальный линт: синтаксические ошибки и неопределенные имена
lint:
	uv run ruff check --select E9,F63,F7,F82 services libs tests bi


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
	@echo "  run-spider         - Любой паук в Docker: SPIDER=autoscout24 MAKES=bmw ARGS=\"-a countries=DE\""
	@echo "  probe              - Проверка паука на живом сайте без Kafka: SPIDER=autovit MAKES=dacia"
	@echo ""
	@echo "Логи:"
	@echo "  logs-ingestor | logs-scheduler | logs-api | logs-kafka | logs-db"
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
	@echo "Аналитика:"
	@echo "  bi-up              - Запуск Metabase (http://localhost:3000)"
	@echo "  bi-provision       - Создать/обновить дашборды Metabase"
	@echo "  stats-backfill     - Пересчёт витрины сегментов (FROM=… TO=…)"
	@echo ""
	@echo "Тестирование:"
	@echo "  test               - Запуск всех тестов"
	@echo "  test-warnings      - Запуск тестов с показом предупреждений"
	@echo "  test-quiet         - Запуск тестов без предупреждений"
	@echo "  test-strict        - Запуск тестов с ошибками на предупреждения"
	@echo "  test-verbose       - Подробный запуск тестов"
	@echo "  test-coverage      - Запуск тестов с покрытием кода"
	@echo "  e2e                - Сквозная проверка на запущенном стеке (make dc-up)"
	@echo "  lint               - Минимальный линт (синтаксис, неопределенные имена)"
