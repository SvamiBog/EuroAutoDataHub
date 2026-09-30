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
scheduler ──► Scrapy (otomoto) ──► Kafka ──► ingestor ──► PostgreSQL ──► FastAPI (/api/v1, аналитика)
 (каждый день)   │                  │ listing_observations      ▲     │
                 │                  │ crawl_events              │     └──► Metabase (дашборды)
                 └── run_id,        └ ingest_dlq (ошибки)       │
                     шарды              lifecycle, курсы ЕЦБ, отчёт о прогоне,
                                        дневные наблюдения, витрина сегментов
```

| Сервис | Путь | Назначение |
|--------|------|------------|
| `scheduler` | `services/scrapy_spiders/car_scrapers` | Ежедневно в `CRAWL_AT` запускает обходы |
| `scrapy_runner` | `services/scrapy_spiders/car_scrapers` | Паук otomoto (GraphQL API) для ручного запуска. Публикует наблюдения объявлений и события обхода (`run_started`, `shard_finished`, `run_finished`) |
| `ingestor` | `services/data_processor` | Пишет наблюдения и события в БД батчами. Ведёт журнал изменений объявлений и снимает объявления с публикации только по полным обходам. Загружает курсы ЕЦБ и строит отчёт о прогоне |
| `api_service` | `services/api_service` | FastAPI: объявления, журнал изменений, статистика и аналитика в EUR; миграции Alembic |
| `metabase` | `bi/metabase` | Дашборды «Рынок», «Сегмент», «Объявление», «Здоровье сбора» (профиль `bi`, дашборды как код) |
| `eadh_common` | `libs/eadh_common` | Общая модель данных, контракт сообщений Kafka, настройки БД |

Главные правила данных:
- объявление определяется парой «площадка + ID на площадке»;
- обход делится на шарды (марка; большие марки — по диапазонам лет);
- объявление снимается с публикации, только если его не было в **двух подряд полных** обходах его шарда;
- объявление, которое снова появилось на сайте, возвращается в продажу с записью `relisted` в журнале.

## Быстрый старт (Docker)

Нужны Docker и Docker Compose v2.

```bash
cp .env.example .env          # задайте пароль PostgreSQL, при желании TELEGRAM_*
make dc-build
make dc-up                    # PostgreSQL, Kafka, миграции, ingestor, API, планировщик
make db-seed-makes            # справочник марок (необязательно: заполняется и по ходу приёма)
make run-oto MAKES=audi,bmw   # разовый обход выбранных марок (без MAKES — все марки)
```

- API: http://localhost:8000/docs
- Логи: `make logs-ingestor`, `make logs-scheduler`, `make logs-api`
- Отчёт о последнем прогоне: таблица `crawl_run`, поле `report`

## Аналитика

**Дашборды (Metabase).** Задайте `MB_ADMIN_*` в `.env` и выполните:

```bash
make bi-up          # Metabase на http://localhost:3000
make bi-provision   # подключение к БД, вопросы и дашборды (повторный запуск обновляет их)
```

**API** (`/api/v1/analytics`, ключ в заголовке `X-API-Key`, если заданы `API_KEYS`):

| Эндпоинт | Что возвращает |
|----------|----------------|
| `/price-trend` | Медиана и квартили цены (EUR) по дням, неделям или месяцам и странам. Фильтры: марка, модель, годы, пробег, топливо, КПП |
| `/segments`, `/segments/timeseries` | Витрина сегментов: предложение (активные, новые, снятые), цены, срок экспозиции |
| `/depreciation` | Цена по возрасту автомобиля |
| `/delisted` | Снятые за период: срок экспозиции, доля со снижением цены, медианная скидка |

Пример: медиана Toyota Corolla 2019–2021 с пробегом 50–100 тыс. км в PL и DE по неделям:
`/api/v1/analytics/price-trend?make=toyota&model=corolla&year_from=2019&year_to=2021&mileage_from=50000&mileage_to=100000&country=PL&country=DE`

Витрина пересчитывается после каждого прогона. Пересчёт за период: `make stats-backfill FROM=2026-09-01 TO=2026-09-30`.

![Дашборд «Сегмент» (демо-данные)](docs/images/metabase_segment.png)

## Локальная разработка

Нужен [uv](https://docs.astral.sh/uv/). Python 3.11 он установит сам.

```bash
uv sync                          # зависимости всех сервисов + dev-инструменты
make test                        # тесты всех сервисов
make lint                        # минимальный линт
make e2e                         # сквозной сценарий на запущенном стеке (make dc-up)

# тесты на настоящем PostgreSQL (витрины, аналитика): создают и удаляют временные БД
TEST_DATABASE_URL=postgresql+asyncpg://postgres:пароль@localhost:5433/postgres make test

set -a; source .env; set +a      # переменные окружения для локальных запусков
make run-oto-local MAKES=audi    # паук с хоста (Kafka на localhost:9094)
make api-dev                     # API с автоперезагрузкой
```

`make e2e` прогоняет четыре «дня» обхода против фейкового сайта (источник `e2e.test`) и проверяет:
- снятие объявления после двух полных обходов;
- что неполный обход ничего не меняет;
- возврат объявления в продажу;
- дробление марки по годам.

## Настройки

Полный список — в `.env.example`.

| Переменная | По умолчанию | Смысл |
|------------|--------------|-------|
| `CRAWL_AT`, `CRAWL_TZ` | 02:00, Europe/Warsaw | Время ежедневного обхода |
| `SCRAPY_CONCURRENT_REQUESTS_PER_DOMAIN` | 8 | Параллельных запросов к сайту |
| `SCRAPY_CONSECUTIVE_403_LIMIT` / `SCRAPY_PAUSE_DURATION` / `SCRAPY_MAX_PAUSES` | 3 / 300 / 5 | Пауза после серии 403 и остановка при устойчивой блокировке |
| `SCRAPY_MAX_PAGES_PER_SHARD` | 500 | Если страниц больше, марка делится по годам |
| `SCRAPY_MIN_MAKE_COMPLETENESS` | 0.95 | Доля собранных от `totalCount`, чтобы шард считался полным |
| `DELIST_AFTER_MISSED_RUNS` | 2 | Сколько полных обходов подряд без объявления нужно для снятия |
| `MAX_DELIST_RATIO` | 0.3 | Предохранитель: не снимать, если «пропало» больше этой доли шарда |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Отправка отчёта о прогоне в Telegram |
| `API_KEYS` | — | Ключи доступа к `/api/v1` через запятую; пусто — доступ без ключа |
| `MB_ADMIN_EMAIL`, `MB_ADMIN_PASSWORD` | — | Администратор Metabase для `make bi-provision` |
