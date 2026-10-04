# Запуск на своём компьютере

> Связанные документы: [README](../README.md) · [Площадки](SOURCES.md) · [Роадмап](ROADMAP.md)

Весь стек работает в Docker: PostgreSQL, Kafka, приём данных, API, планировщик обходов и Metabase. Для запуска
Python не нужен. [uv](https://docs.astral.sh/uv/) нужен для проверки площадок (`make probe`), тестов и разработки.

## 1. Что установить

| Система | Установка |
|---------|-----------|
| Windows | Docker Desktop с WSL 2. В Docker Desktop: Settings → Resources → WSL Integration → включить Ubuntu. Все команды ниже выполняются в терминале Ubuntu (WSL), не в PowerShell; там же: `sudo apt update && sudo apt install -y git make`. Репозиторий клонируется в домашнюю папку WSL, а не на диск `C:` |
| macOS | Docker Desktop; git и make: `xcode-select --install` |
| Linux | Docker Engine с Compose v2, git, make |

- **Память:** выделите Docker 4 ГБ (в Docker Desktop: Settings → Resources). В простое стек с Metabase занимает
  около 1,5 ГБ; во время обхода и работы с дашбордами — больше.
- **Порты:** 5433 (PostgreSQL), 9094 (Kafka), 8000 (API), 3000 (Metabase) должны быть свободны.

### uv: отдельное окружение Python

uv ставит Python 3.11 и зависимости проекта в папку `.venv` внутри репозитория. Системный Python, пакеты
и другие проекты на компьютере он не меняет. Чтобы удалить окружение, достаточно удалить `.venv`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # Linux, macOS, WSL; без sudo, в ~/.local/bin
# на macOS можно и так: brew install uv
```

Откройте новый терминал и проверьте: `uv --version`.

## 2. Скачать и настроить

```bash
git clone https://github.com/SvamiBog/EuroAutoDataHub.git
cd EuroAutoDataHub
cp .env.example .env
uv sync        # окружение .venv: Python 3.11 (скачается, если его нет) и зависимости проекта
```

В `.env` задайте свои `POSTGRES_PASSWORD` и `MB_ADMIN_PASSWORD`. Пароль PostgreSQL применяется при первом
запуске базы: если поменять его потом, базу придётся пересоздать (`make dc-fresh`, данные удалятся).

## 3. Запуск

```bash
make dc-build      # сборка образов, несколько минут
make dc-up         # PostgreSQL, Kafka, миграции, приём данных, API, планировщик
make status        # сервисы в статусе Up
make api-test      # ответ со "status":"healthy"
```

## 4. Первый обход

Начните с 2–3 марок: полный обход otomoto.pl — сотни тысяч объявлений.

```bash
make run-oto MAKES=alfa-romeo,dacia
make logs-ingestor        # в другом окне: как объявления записываются в базу
```

Если площадка отвечает 403, паук сам делает паузу (5 минут) и останавливается при устойчивой блокировке. Сразу
повторять не нужно: уменьшите `SCRAPY_CONCURRENT_REQUESTS_PER_DOMAIN` в `.env` или подключите прокси
(README, раздел «Прокси»).

## 5. Дашборды и API

```bash
make bi-up          # Metabase стартует 1–2 минуты
make bi-provision   # подключение к БД и 5 дашбордов; повторный запуск обновляет их
```

- Metabase: http://localhost:3000, вход — `MB_ADMIN_EMAIL` и `MB_ADMIN_PASSWORD` из `.env`.
- API с документацией: http://localhost:8000/docs.

## 6. Ежедневный обход

Планировщик запускает обход каждый день в `CRAWL_AT` (по умолчанию 02:00, `CRAWL_TZ` — Europe/Warsaw), если
компьютер включён и Docker запущен. Ограничить марки: `CRAWL_ARGS=-a makes=alfa-romeo,dacia` в `.env`, затем
`make dc-up`.

## Что и когда появится

| Когда | Что видно |
|-------|-----------|
| После первого обхода | Объявления, цены в EUR (PLN пересчитываются по курсу ЕЦБ), отчёт о запуске на дашборде «Здоровье сбора», справедливая цена и «ниже рынка» в сегментах, где хватает объявлений |
| Со второго дня | Изменения цен; снятые объявления (снятие — после двух полных обходов подряд без объявления), срок экспозиции |
| Когда наберётся 2000+ объявлений и 200+ новых за 2 недели | Модель справедливой цены (интервал P10–P90, deal score) и арбитраж между странами; до этого справедливая цена считается по сегменту. Проверить: `make ml-status` |
| Через несколько недель | Сдвиги цены и предложения сегментов: они сравниваются со скользящей медианой за 28 дней. Модель срока до снятия: ей нужны объявления, за которыми наблюдали 1–2 месяца |

## Другие площадки

autovit.ro, standvirtual.com и AutoScout24 перед включением проверяются на живом сайте. Проверка запускается
через uv, без Kafka и базы:

```bash
make probe SPIDER=autovit MAKES=dacia
make probe SPIDER=standvirtual MAKES=renault
make probe SPIDER=autoscout24 MAKES=bmw ARGS="-a countries=DE" PAGES=1
```

Отчёт проверяет, что есть полный шард с объявлениями, поля заполнены у 90 %+ объявлений и нет ошибок API.
Если всё прошло, добавьте паука в `CRAWL_SPIDERS` в `.env` и выполните `make dc-up`. Разовый обход в Docker:
`make run-spider SPIDER=autovit MAKES=dacia`. Подробности — в [SOURCES.md](SOURCES.md).

## Алерты в Telegram

Задайте `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в `.env` и выполните `make dc-up`. Без них отчёты и алерты
пишутся в лог (`make logs-ingestor`).

## Остановка и удаление

```bash
make dc-down        # остановить; данные остаются в томах Docker
make dc-fresh       # пересоздать всё с нуля: данные удаляются
rm -rf .venv        # удалить окружение Python проекта
```

Удалить сам uv: `uv cache clean`, затем `rm ~/.local/bin/uv ~/.local/bin/uvx`.

## Если что-то не работает

- **Состояние и логи:** `make status`, `make logs-ingestor`, `make logs-scheduler`, `make logs-api`; отчёт
  о запуске — дашборд «Здоровье сбора».
- **`port is already allocated`:** порт занят другой программой. Освободите его или поменяйте левую часть
  в `ports` в `docker-compose.yml`.
- **`429 Too Many Requests` при сборке:** Docker Hub ограничил скачивание образов. Выполните `docker login`
  с бесплатным аккаунтом Docker Hub и повторите `make dc-build`.
