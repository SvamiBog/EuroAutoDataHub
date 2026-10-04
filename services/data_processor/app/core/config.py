# services/data_processor/app/core/config.py
from typing import Optional

from pydantic import Field

from eadh_common.messages import TOPIC_CRAWL_EVENTS, TOPIC_DLQ, TOPIC_LISTING_OBSERVATIONS
from eadh_common.settings import DatabaseSettings


class Settings(DatabaseSettings):
    """
    Настройки ingestor. Значения загружаются из переменных окружения или файла .env.
    Параметры PostgreSQL (POSTGRES_*) — в DatabaseSettings.
    """

    # --- Kafka ---
    KAFKA_BOOTSTRAP_SERVERS: str = Field(default="localhost:9092")
    KAFKA_TOPIC_OBSERVATIONS: str = Field(default=TOPIC_LISTING_OBSERVATIONS)
    KAFKA_TOPIC_CRAWL_EVENTS: str = Field(default=TOPIC_CRAWL_EVENTS)
    KAFKA_TOPIC_DLQ: str = Field(default=TOPIC_DLQ)
    KAFKA_CONSUMER_GROUP: str = Field(default="ingestor")

    # --- Приём сообщений ---
    INGEST_BATCH_SIZE: int = Field(default=1000)
    INGEST_POLL_TIMEOUT_MS: int = Field(default=1000)
    # Максимальная пауза между попытками записи, если БД недоступна
    DB_RETRY_MAX_DELAY_S: float = Field(default=60.0)

    # --- Жизненный цикл объявлений ---
    LIFECYCLE_INTERVAL_S: float = Field(default=60.0)
    # Объявление снимается после стольких полных обходов шарда подряд, в которых его не было
    DELIST_AFTER_MISSED_RUNS: int = Field(default=2)
    # Предохранитель: если за один полный обход «пропало» больше этой доли активных объявлений шарда,
    # снятие не применяется (вероятна ошибка парсера)
    MAX_DELIST_RATIO: float = Field(default=0.3)
    MIN_ADS_FOR_DELIST_GUARD: int = Field(default=20)
    # Сколько ждать, пока ingestor догонит наблюдения полного шарда, прежде чем пропустить его
    LIFECYCLE_WAIT_TIMEOUT_H: float = Field(default=6.0)

    # --- Курсы валют ЕЦБ ---
    FX_URL: str = Field(default="https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml")
    FX_REFRESH_INTERVAL_H: float = Field(default=6.0)

    # --- Отчёт о прогоне ---
    TELEGRAM_BOT_TOKEN: Optional[str] = Field(default=None)
    TELEGRAM_CHAT_ID: Optional[str] = Field(default=None)
    # Отчёт отправляется, когда все полные шарды запуска обработаны, но не позже этого срока
    REPORT_WAIT_TIMEOUT_H: float = Field(default=6.0)

    # --- Здоровье сбора (правила по запуску обхода) ---
    # Объём запуска сравнивается с медианой запусков той же площадки за столько дней
    HEALTH_BASELINE_DAYS: int = Field(default=7)
    # Алерт, если собрано на эту долю меньше медианы (при падении вдвое — критический)
    HEALTH_VOLUME_DROP: float = Field(default=0.3)
    HEALTH_MIN_COMPLETENESS: float = Field(default=0.95)
    # Доля неполных шардов, начиная с которой алерт критический
    HEALTH_INCOMPLETE_CRITICAL: float = Field(default=0.2)
    HEALTH_403_ALERT: int = Field(default=20)
    # Запросов, окончательно упавших из-за ошибок API (GraphQL, HTTP, JSON), для предупреждения
    HEALTH_API_ERRORS_ALERT: int = Field(default=3)
    # Падение заполненности поля (доля объявлений) относительно обычной — признак смены формата ответа
    HEALTH_FILL_DROP: float = Field(default=0.2)
    # Доля объявлений запуска с нарушениями качества данных для предупреждения
    HEALTH_QUALITY_SHARE: float = Field(default=0.05)
    # Нет ни одного запуска площадки дольше этого — алерт
    HEALTH_MAX_RUN_GAP_H: float = Field(default=36.0)
    # Запуск идёт дольше этого — алерт о зависшем обходе
    HEALTH_RUN_MAX_DURATION_H: float = Field(default=20.0)

    # --- Качество данных (правила для объявления) ---
    DQ_MIN_PRICE_EUR: float = Field(default=500.0)
    DQ_MAX_PRICE_EUR: float = Field(default=3_000_000.0)
    DQ_MAX_MILEAGE_KM: int = Field(default=2_000_000)
    DQ_MIN_POWER_HP: int = Field(default=20)
    DQ_MAX_POWER_HP: int = Field(default=2000)

    # --- Ценовые аномалии и справедливая цена ---
    # Минимум объявлений в сегменте сравнения; меньше — сегмент укрупняется
    PRICE_MIN_SEGMENT: int = Field(default=30)
    PRICE_Z_THRESHOLD: float = Field(default=3.0)
    # Отклонение от справедливой цены, начиная с которого объявление — аномалия
    PRICE_MIN_DEVIATION: float = Field(default=0.25)
    # Дешевле справедливой цены на эту долю и больше — неправдоподобная цена (качество данных), а не выгодное предложение
    PRICE_IMPLAUSIBLE_DISCOUNT: float = Field(default=0.6)

    # --- Поведенческие аномалии ---
    BEHAVIOR_RELIST_WINDOW_DAYS: int = Field(default=30)
    BEHAVIOR_PRICE_CHANGES: int = Field(default=4)
    BEHAVIOR_PRICE_CHANGES_DAYS: int = Field(default=14)
    # Уменьшение пробега меньше этого не считается скручиванием (опечатки, округление)
    BEHAVIOR_MILEAGE_TOLERANCE_KM: int = Field(default=1000)

    # --- Рыночные аномалии (ряды витрины segment_daily_stats) ---
    MARKET_WINDOW_DAYS: int = Field(default=28)
    MARKET_MIN_HISTORY: int = Field(default=14)
    MARKET_K: float = Field(default=3.5)
    MARKET_MIN_PRICE_SHIFT: float = Field(default=0.07)
    MARKET_MIN_SUPPLY_SHIFT: float = Field(default=0.15)
    MARKET_MIN_VOLUME: int = Field(default=30)

    # --- Дайджест «ниже рынка» ---
    DIGEST_MAX_ITEMS: int = Field(default=20)
    # Дайджест по подписке отправляется не чаще, чем раз в столько часов
    DIGEST_MIN_INTERVAL_H: float = Field(default=20.0)

    # --- ML (этап 5): модель справедливой цены и срока до снятия ---
    ML_ENABLED: bool = Field(default=True)
    # Переобучение не чаще, чем раз в столько дней (после прогона)
    ML_RETRAIN_DAYS: float = Field(default=7.0)
    # Если модель не обучилась (мало данных), следующая попытка — не раньше чем через столько часов
    ML_RETRY_HOURS: float = Field(default=12.0)
    # Обучающая выборка — объявления, появившиеся за столько дней
    ML_TRAIN_WINDOW_DAYS: int = Field(default=365)
    # Проверка по времени: объявления, появившиеся за последние столько дней, откладываются
    ML_VALID_DAYS: int = Field(default=14)
    # Доля самых поздних обучающих объявлений для ранней остановки и калибровки интервала
    ML_CALIB_SHARE: float = Field(default=0.15)
    ML_MIN_TRAIN_ROWS: int = Field(default=2000)
    ML_MIN_VALID_ROWS: int = Field(default=200)
    # Минимум общих объявлений, чтобы сравнить модель с v1 и с действующей моделью
    ML_MIN_COMPARE_ROWS: int = Field(default=100)
    # Второй проход обучения без объявлений, чья цена дальше от P50 первого прохода, чем на столько MAD
    ML_OUTLIER_MAD: float = Field(default=3.5)
    # Категория (модель, топливо…) встречается реже — считается неизвестной
    ML_MIN_CATEGORY_COUNT: int = Field(default=5)
    # Проверка качества: MAPE модели не больше MAPE v1 × это; не хуже действующей модели больше чем на 2 %
    ML_MAX_MAPE_RATIO: float = Field(default=1.0)
    ML_MAX_MAPE_REGRESSION: float = Field(default=1.02)
    # Доля цен внутри интервала P10–P90 на отложенной выборке (в идеале 80 %)
    ML_COVERAGE_MIN: float = Field(default=0.7)
    ML_COVERAGE_MAX: float = Field(default=0.9)
    # Потоков LightGBM; 0 — половина ядер (все ядра брать нельзя, см. app/ml/executor.py)
    ML_THREADS: int = Field(default=0)
    # Примеров той же модели в обучении меньше — объявление оценивается по v1 (медиана сегмента)
    ML_PRICE_MIN_SUPPORT: int = Field(default=20)
    # Аномалия по модели: |z| (log-цена против P50 в «сигмах» интервала) не меньше этого. Интервал модели шире
    # разброса v1 (в нём и шум цен продавцов), поэтому порог ниже, чем PRICE_Z_THRESHOLD; z = −2 — дешевле
    # 98 % похожих объявлений
    PRICE_MODEL_Z_THRESHOLD: float = Field(default=2.0)
    # Срок до снятия: горизонты (дни) и минимальный ROC AUC на отложенной выборке
    ML_DOM_HORIZONS: list[int] = Field(default=[7, 14, 30, 60])
    ML_DOM_MIN_AUC: float = Field(default=0.6)
    ML_DOM_MIN_ROWS: int = Field(default=500)

    # --- Межстрановой арбитраж (этап 5.3) ---
    ARBITRAGE_ENABLED: bool = Field(default=True)
    # Модель издержек: JSON-строка или путь к JSON-файлу (см. docs/ML.md); пусто — значения по умолчанию
    ARBITRAGE_COSTS: Optional[str] = Field(default=None)
    ARBITRAGE_COSTS_FILE: Optional[str] = Field(default=None)
    ARBITRAGE_MIN_PROFIT_EUR: float = Field(default=1000.0)
    ARBITRAGE_MIN_ROI: float = Field(default=0.08)
    # Прибыль и по осторожной цене продажи (P10) должна быть не меньше нуля
    ARBITRAGE_REQUIRE_P10_PROFIT: bool = Field(default=True)
    # Примеров той же модели в стране продажи в обучении модели — иначе прогноз цены там ненадёжен
    ARBITRAGE_MIN_COMPARABLES: int = Field(default=20)


# Создаем экземпляр настроек, который будет использоваться в других модулях
settings = Settings()
