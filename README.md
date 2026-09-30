# EuroAutoDataHub

Ежедневный сбор объявлений о продаже подержанных автомобилей с европейских площадок,
история каждого объявления, рыночная аналитика и поиск аномалий.

Сейчас подключена площадка **otomoto.pl** (Польша). План развития — в [роадмапе](docs/ROADMAP.md).

| Документ | Содержание |
|----------|------------|
| [docs/PRD.md](docs/PRD.md) | Требования к продукту: цели, пользователи, данные, аналитика, аномалии, архитектура |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Этапы разработки и журнал выполнения |
| [docs/AUDIT.md](docs/AUDIT.md) | Аудит кода: найденные ошибки и где они исправляются |

## Архитектура

```
Scrapy (otomoto) ──► Kafka ──► data_processor  ──► PostgreSQL ──► FastAPI
                     │        (объявления)          ▲
                     └──────► status_updater ───────┘
                              (снятие с публикации по полному обходу марки)
```

| Сервис | Путь | Назначение |
|--------|------|------------|
| `scrapy_runner` | `services/scrapy_spiders/car_scrapers` | Паук otomoto (GraphQL API), публикует объявления и списки активных ID в Kafka |
| `data_processor` | `services/data_processor` | Консьюмер `parsed_car_ads`: создает и обновляет объявления, ведет историю цен |
| `status_updater` | `services/data_processor` | Консьюмер `active_car_ids`: снимает с публикации пропавшие объявления, только по полностью собранной марке |
| `api_service` | `services/api_service` | FastAPI: объявления, история, статистика; миграции Alembic |

## Быстрый старт (Docker)

Нужны Docker и Docker Compose v2.

```bash
cp .env.example .env          # задайте пароль PostgreSQL
make dc-build
make dc-up                    # PostgreSQL, Kafka, консьюмеры, API
make db-upgrade               # миграции
make run-oto MAKES=audi,bmw   # обход выбранных марок (без MAKES — все марки)
```

- API: http://localhost:8000/docs
- Проверка: `make api-test`
- Логи: `make logs-processor`, `make logs-updater`, `make logs-api`

## Локальная разработка

Нужен [uv](https://docs.astral.sh/uv/). Python 3.11 он установит сам.

```bash
uv sync                          # зависимости всех сервисов + dev-инструменты
make test                        # тесты всех сервисов
make lint                        # минимальный линт

set -a; source .env; set +a      # переменные окружения для локальных запусков
make run-oto-local MAKES=audi    # паук с хоста (Kafka на localhost:9094)
make api-dev                     # API с автоперезагрузкой
```

## Настройки парсера

Настройки задаются переменными окружения `SCRAPY_*` (полный список — в `.env.example`).

| Переменная | По умолчанию | Смысл |
|------------|--------------|-------|
| `SCRAPY_CONCURRENT_REQUESTS_PER_DOMAIN` | 8 | Параллельных запросов к сайту |
| `SCRAPY_CONSECUTIVE_403_LIMIT` | 3 | После стольких 403 подряд обход встает на паузу |
| `SCRAPY_PAUSE_DURATION` | 300 | Длительность паузы, секунд |
| `SCRAPY_MAX_PAUSES` | 5 | После стольких пауз обход останавливается |
| `SCRAPY_MIN_MAKE_COMPLETENESS` | 0.95 | Минимальная доля собранных объявлений от `totalCount`, чтобы марка считалась собранной полностью |

Объявления снимаются с публикации **только** по марке, собранной полностью.
Дополнительно status_updater не снимает больше `MAX_DELIST_RATIO` (30 %) активных объявлений марки за раз.
