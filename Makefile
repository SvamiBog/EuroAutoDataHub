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
	uv run docker-compose build --no-cache
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
	@echo "--- Перехожу в $(SCRAPY_DIR) и запускаю Scrapy Docker ---"
	@cd $(SCRAPY_DIR) && docker-compose run --rm scrapy_runner scrapy crawl otomoto

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
