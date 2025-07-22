# EuroAutoDataHub Makefile

# Docker Compose команды
dc-up:
	uv run docker-compose up -d

dc-down:
	uv run docker-compose down

dc-build:
	uv run docker-compose build

dc-restart:
	@echo "--- Перезапуск всех сервисов ---"
	uv run docker-compose down
	uv run docker-compose up -d

dc-rebuild:
	@echo "--- Остановка, удаление, пересборка и запуск всех сервисов ---"
	uv run docker-compose down
	uv run docker-compose build
	uv run docker-compose up -d

dc-rebuild-service:
	@echo "--- Пересборка конкретного сервиса (использование: make dc-rebuild-service SERVICE=имя_сервиса) ---"
	uv run docker-compose stop $(SERVICE)
	uv run docker-compose build --no-cache $(SERVICE)
	uv run docker-compose up -d $(SERVICE)

dc-fresh:
	@echo "--- Полная очистка и пересборка (удаляет volumes и images) ---"
	uv run docker-compose down -v --remove-orphans
	uv run docker-compose build --no-cache --pull
	uv run docker-compose up -d

# Scrapy команды
SCRAPY_DIR = services/scrapy_spiders/car_scrapers/car_scrapers

run-oto-local:
	@echo "--- Перехожу в $(SCRAPY_DIR) и запускаю Scrapy Local ---"
	@cd $(SCRAPY_DIR) && uv run scrapy crawl otomoto

run-oto-docker:
	@echo "--- Запуск Scrapy через главный docker-compose ---"
	docker-compose run --rm scrapy_runner scrapy crawl otomoto

# Логи сервисов
logs-processor:
	uv run docker-compose logs -f data_processor

logs-updater:
	uv run docker-compose logs -f status_updater

logs-api:
	uv run docker-compose logs -f api_service_app

logs-kafka:
	uv run docker-compose logs -f kafka_broker

logs-db:
	uv run docker-compose logs -f db_postgres

# API команды
api-dev:
	cd services/api_service && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

api-test:
	curl -X GET "http://localhost:8000/health" -H "accept: application/json"

api-docs:
	@echo "API Documentation available at: http://localhost:8000/docs"
	@echo "Redoc Documentation available at: http://localhost:8000/redoc"

# Миграции базы данных
db-upgrade:
	uv run docker-compose run --rm api_migrations alembic upgrade head

db-revision:
	uv run docker-compose run --rm api_migrations alembic revision --autogenerate -m "$(msg)"

# Проверка статуса сервисов
status:
	@echo "--- Статус всех сервисов ---"
	uv run docker-compose ps

# Основная команда для pytest
.PHONY: test test-pytest test-unittest test-verbose test-coverage test-warnings
test:
	@echo "--- 🚀 Запуск всех тестов через pytest ---"
	PYTHONPATH=. uv run pytest services/ -v 

test-warnings:
	@echo "--- 🚨 Запуск тестов с показом предупреждений ---"
	PYTHONPATH=. uv run pytest services/ -v -s --tb=short

test-strict:
	@echo "--- 🚫 Запуск тестов с ошибками на предупреждения ---"
	PYTHONPATH=. uv run pytest services/ -v -W error::DeprecationWarning

test-coverage:
	@echo "--- 📊 Запуск тестов с покрытием ---"
	PYTHONPATH=. uv run pytest services/ -v --cov=services --cov-report=html

test-quiet:
	@echo "--- 🤫 Запуск тестов без предупреждений ---"
	PYTHONPATH=. uv run pytest services/ -v --disable-warnings

test-verbose:
	@echo "--- 📝 Подробный запуск тестов ---"
	PYTHONPATH=. uv run pytest services/ -vv -s --tb=long


# Помощь
help:
	@echo "Доступные команды:"
	@echo "  dc-up              - Запуск всех сервисов"
	@echo "  dc-down            - Остановка всех сервисов"
	@echo "  dc-build           - Сборка всех сервисов"
	@echo "  dc-rebuild         - Остановка, сборка и запуск"
	@echo "  dc-restart         - Перезапуск всех сервисов"
	@echo "  dc-rebuild-all     - Полная пересборка без кеша"
	@echo "  dc-fresh           - Полная очистка и пересборка"
	@echo "  restart-scrapy     - Перезапуск только Scrapy"
	@echo "  restart-processor  - Перезапуск только Data Processor"
	@echo "  restart-updater    - Перезапуск только Status Updater"
	@echo "  run-oto-docker     - Запуск парсера Otomoto в Docker"
	@echo "  status             - Показать статус всех сервисов"
	@echo "  logs-all           - Показать логи всех сервисов"
	@echo "  clean              - Очистить неиспользуемые ресурсы"
	@echo ""
	@echo "API команды:"
	@echo "  api-dev            - Запуск API в режиме разработки"
	@echo "  api-test           - Проверка работы API"
	@echo "  api-docs           - Информация о документации API"
	@echo ""
	@echo "База данных:"
	@echo "  db-upgrade         - Применение миграций"
	@echo "  db-revision        - Создание новой миграции"
	@echo ""
	@echo "Тестирование:"
	@echo "  test               - Запуск всех тестов"
	@echo "  test-warnings      - Запуск тестов с показом предупреждений"
	@echo "  test-quiet         - Запуск тестов без предупреждений"
	@echo "  test-strict        - Запуск тестов с ошибками на предупреждения"
	@echo "  test-verbose       - Подробный запуск тестов"
	@echo "  test-coverage      - Запуск тестов с покрытием кода"
