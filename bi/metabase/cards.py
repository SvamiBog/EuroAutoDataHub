"""Вопросы (native SQL) и дашборды Metabase для EuroAutoDataHub.

Фильтры дашбордов связаны с переменными SQL ({{make}} и т.п.); необязательные условия — в [[ ... ]].
"""

# Переменные SQL: имя -> (подпись, тип переменной Metabase, тип фильтра дашборда)
VARIABLES = {
    "country": ("Страна", "text", "string/="),
    "make": ("Марка", "text", "string/="),
    "model": ("Модель", "text", "string/="),
    "year_from": ("Год от", "number", "number/="),
    "year_to": ("Год до", "number", "number/="),
    "mileage_from": ("Пробег от, км", "number", "number/="),
    "mileage_to": ("Пробег до, км", "number", "number/="),
    "date_from": ("С даты", "date", "date/single"),
    "date_to": ("По дату", "date", "date/single"),
    "source": ("Площадка", "text", "string/="),
    "listing": ("ID объявления на площадке", "text", "string/="),
}

LATEST_STATS = "(SELECT max(stat_date) FROM segment_daily_stats)"

# Фильтры по характеристикам объявления l (listing) с каноничными марками vm и моделями vmo
LISTING_FILTERS = """
      [[AND vm.slug = {{make}}]]
      [[AND vmo.slug = {{model}}]]
      [[AND l.country_code = {{country}}]]
      [[AND l.year >= {{year_from}}]]
      [[AND l.year <= {{year_to}}]]"""

CARDS = {
    # --- Рынок ---
    "market_countries": {
        "name": "Рынок: страны за последний день",
        "display": "table",
        "sql": f"""
SELECT country_code AS "Страна", active_count AS "Активных", new_count AS "Новых", delisted_count AS "Снято",
       price_eur_median AS "Медиана цены, EUR", dom_median_days AS "Срок экспозиции, дн.",
       price_drop_count AS "Снижений цены", stat_date AS "Дата"
FROM segment_daily_stats
WHERE level = 'country' AND stat_date = {LATEST_STATS}
  [[AND country_code = {{{{country}}}}]]
ORDER BY active_count DESC""",
    },
    "market_active": {
        "name": "Рынок: активные объявления",
        "display": "line",
        "sql": """
SELECT stat_date, country_code, active_count
FROM segment_daily_stats
WHERE level = 'country'
  [[AND country_code = {{country}}]]
  [[AND stat_date >= {{date_from}}]]
  [[AND stat_date <= {{date_to}}]]
ORDER BY stat_date, country_code""",
        "viz": {"graph.dimensions": ["stat_date", "country_code"], "graph.metrics": ["active_count"],
                "graph.x_axis.title_text": "Дата", "graph.y_axis.title_text": "Активных объявлений"},
    },
    "market_flow": {
        "name": "Рынок: новые и снятые за день",
        "display": "bar",
        "sql": """
SELECT stat_date, sum(new_count) AS new_count, sum(delisted_count) AS delisted_count
FROM segment_daily_stats
WHERE level = 'country'
  [[AND country_code = {{country}}]]
  [[AND stat_date >= {{date_from}}]]
  [[AND stat_date <= {{date_to}}]]
GROUP BY stat_date
ORDER BY stat_date""",
        "viz": {"graph.dimensions": ["stat_date"], "graph.metrics": ["new_count", "delisted_count"],
                "graph.x_axis.title_text": "Дата", "graph.y_axis.title_text": "Объявлений",
                "series_settings": {"new_count": {"title": "Новые"}, "delisted_count": {"title": "Снятые"}}},
    },
    "market_price": {
        "name": "Рынок: медианная цена, EUR",
        "display": "line",
        "sql": """
SELECT stat_date, country_code, price_eur_median
FROM segment_daily_stats
WHERE level = 'country' AND observed_count > 0
  [[AND country_code = {{country}}]]
  [[AND stat_date >= {{date_from}}]]
  [[AND stat_date <= {{date_to}}]]
ORDER BY stat_date, country_code""",
        "viz": {"graph.dimensions": ["stat_date", "country_code"], "graph.metrics": ["price_eur_median"],
                "graph.x_axis.title_text": "Дата", "graph.y_axis.title_text": "Медиана цены, EUR"},
    },
    "market_top_makes": {
        "name": "Рынок: топ марок по числу активных",
        "display": "bar",
        "sql": f"""
SELECT COALESCE(vm.name, 'не определена') AS make, sum(s.active_count) AS active_count
FROM segment_daily_stats s
LEFT JOIN vehicle_make vm ON vm.id = s.make_id
WHERE s.level = 'make' AND s.stat_date = {LATEST_STATS}
  [[AND s.country_code = {{{{country}}}}]]
GROUP BY 1
ORDER BY 2 DESC
LIMIT 15""",
        "viz": {"graph.dimensions": ["make"], "graph.metrics": ["active_count"],
                "graph.x_axis.title_text": "Марка", "graph.y_axis.title_text": "Активных объявлений"},
    },

    # --- Сегмент ---
    "segment_price_trend": {
        "name": "Сегмент: медиана цены по неделям, EUR",
        "display": "line",
        # каждое объявление учитывается один раз за неделю — по последнему наблюдению недели
        "sql": f"""
WITH per_listing AS (
    SELECT DISTINCT ON (date_trunc('week', o.obs_date), o.listing_id)
           date_trunc('week', o.obs_date)::date AS week, l.country_code, o.price_eur
    FROM listing_observation o
    JOIN listing l ON l.id = o.listing_id
    LEFT JOIN vehicle_make vm ON vm.id = l.make_id
    LEFT JOIN vehicle_model vmo ON vmo.id = l.model_id
    WHERE o.price_eur IS NOT NULL
      [[AND o.obs_date >= {{{{date_from}}}}]]
      [[AND o.obs_date <= {{{{date_to}}}}]]
      [[AND o.mileage_km >= {{{{mileage_from}}}}]]
      [[AND o.mileage_km <= {{{{mileage_to}}}}]]{LISTING_FILTERS}
    ORDER BY date_trunc('week', o.obs_date), o.listing_id, o.obs_date DESC
)
SELECT week, country_code, count(*) AS listings,
       percentile_cont(0.25) WITHIN GROUP (ORDER BY price_eur) AS p25,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY price_eur) AS median,
       percentile_cont(0.75) WITHIN GROUP (ORDER BY price_eur) AS p75
FROM per_listing
GROUP BY week, country_code
ORDER BY week, country_code""",
        "viz": {"graph.dimensions": ["week", "country_code"], "graph.metrics": ["median"],
                "graph.x_axis.title_text": "Неделя", "graph.y_axis.title_text": "Медиана цены, EUR"},
    },
    "segment_supply": {
        "name": "Сегмент: активные объявления",
        "display": "line",
        "sql": """
SELECT s.stat_date, s.country_code, sum(s.active_count) AS active_count
FROM segment_daily_stats s
LEFT JOIN vehicle_make vm ON vm.id = s.make_id
LEFT JOIN vehicle_model vmo ON vmo.id = s.model_id
WHERE s.level = 'model'
  [[AND vm.slug = {{make}}]]
  [[AND vmo.slug = {{model}}]]
  [[AND s.country_code = {{country}}]]
  [[AND s.stat_date >= {{date_from}}]]
  [[AND s.stat_date <= {{date_to}}]]
GROUP BY s.stat_date, s.country_code
ORDER BY s.stat_date, s.country_code""",
        "viz": {"graph.dimensions": ["stat_date", "country_code"], "graph.metrics": ["active_count"],
                "graph.x_axis.title_text": "Дата", "graph.y_axis.title_text": "Активных объявлений"},
    },
    "segment_depreciation": {
        "name": "Сегмент: цена по возрасту автомобиля, EUR",
        "display": "line",
        "sql": f"""
WITH per_listing AS (
    SELECT DISTINCT ON (o.listing_id)
           EXTRACT(YEAR FROM o.obs_date)::int - l.year AS age, l.country_code, o.price_eur
    FROM listing_observation o
    JOIN listing l ON l.id = o.listing_id
    LEFT JOIN vehicle_make vm ON vm.id = l.make_id
    LEFT JOIN vehicle_model vmo ON vmo.id = l.model_id
    WHERE o.price_eur IS NOT NULL AND l.year IS NOT NULL
      [[AND o.obs_date >= {{{{date_from}}}}]]
      [[AND o.obs_date <= {{{{date_to}}}}]]{LISTING_FILTERS}
    ORDER BY o.listing_id, o.obs_date DESC
)
SELECT age, country_code, count(*) AS listings,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY price_eur) AS median
FROM per_listing
WHERE age BETWEEN 0 AND 25
GROUP BY age, country_code
ORDER BY age, country_code""",
        "viz": {"graph.dimensions": ["age", "country_code"], "graph.metrics": ["median"],
                "graph.x_axis.title_text": "Возраст, лет", "graph.y_axis.title_text": "Медиана цены, EUR"},
    },
    "segment_delisted": {
        "name": "Сегмент: снятые — срок экспозиции и снижения цены",
        "display": "table",
        "sql": f"""
SELECT l.country_code AS "Страна", count(*) AS "Снято",
       round(percentile_cont(0.5) WITHIN GROUP (
           ORDER BY EXTRACT(EPOCH FROM (l.last_seen_at - l.first_seen_at)) / 86400)::numeric, 1)
           AS "Медиана срока, дн.",
       count(*) FILTER (WHERE EXISTS (
           SELECT 1 FROM listing_event e
           WHERE e.listing_id = l.id AND e.event_type = 'price_change' AND e.price < e.old_price))
           AS "Со снижением цены"
FROM listing l
LEFT JOIN vehicle_make vm ON vm.id = l.make_id
LEFT JOIN vehicle_model vmo ON vmo.id = l.model_id
WHERE l.status = 'delisted'
  [[AND l.delisted_at >= {{{{date_from}}}}]]
  [[AND l.delisted_at < {{{{date_to}}}}::date + 1]]{LISTING_FILTERS}
GROUP BY l.country_code
ORDER BY 2 DESC""",
    },

    # --- Объявление ---
    "listing_card": {
        "name": "Объявление: карточка",
        "display": "table",
        "sql": """
SELECT source AS "Площадка", source_listing_id AS "ID", title AS "Заголовок", make_name AS "Марка",
       model_name AS "Модель", year AS "Год", mileage_km AS "Пробег", price AS "Цена", currency AS "Валюта",
       price_eur AS "Цена, EUR", status AS "Статус", first_seen_at AS "Впервые", last_seen_at AS "Последний раз",
       delisted_at AS "Снято", days_on_market AS "Дней на рынке", url AS "Ссылка"
FROM v_listing
WHERE 1 = 1
  [[AND source = {{source}}]]
  [[AND source_listing_id = {{listing}}]]
ORDER BY last_seen_at DESC
LIMIT 50""",
    },
    "listing_price_history": {
        "name": "Объявление: цена по дням, EUR",
        "display": "line",
        "sql": """
SELECT o.obs_date, o.price_eur, o.mileage_km
FROM listing_observation o
JOIN listing l ON l.id = o.listing_id
WHERE l.source_listing_id = {{listing}}
  [[AND l.source = {{source}}]]
ORDER BY o.obs_date""",
        "viz": {"graph.dimensions": ["obs_date"], "graph.metrics": ["price_eur"],
                "graph.x_axis.title_text": "Дата", "graph.y_axis.title_text": "Цена, EUR"},
        "required": ["listing"],
    },
    "listing_events": {
        "name": "Объявление: журнал изменений",
        "display": "table",
        "sql": """
SELECT e.ts AS "Когда", e.event_type AS "Событие", e.old_price AS "Было", e.price AS "Стало",
       e.currency AS "Валюта", e.old_mileage_km AS "Пробег был", e.mileage_km AS "Пробег стал"
FROM listing_event e
JOIN listing l ON l.id = e.listing_id
WHERE l.source_listing_id = {{listing}}
  [[AND l.source = {{source}}]]
ORDER BY e.ts DESC""",
        "required": ["listing"],
    },

    # --- Здоровье сбора ---
    "health_runs": {
        "name": "Сбор: последние запуски",
        "display": "table",
        "sql": """
SELECT started_at AS "Начало", source AS "Площадка", duration_min AS "Минут", finish_reason AS "Завершение",
       shards_complete || ' / ' || shards AS "Полных шардов", collected AS "Собрано", expected AS "Ожидалось",
       completeness AS "Полнота", new_listings AS "Новых", delisted AS "Снято", warnings_count AS "Предупреждений",
       warnings AS "Предупреждения"
FROM v_crawl_health
WHERE 1 = 1
  [[AND source = {{source}}]]
ORDER BY started_at DESC
LIMIT 30""",
    },
    "health_completeness": {
        "name": "Сбор: полнота по запускам",
        "display": "line",
        "sql": """
SELECT started_at, source, completeness, collected
FROM v_crawl_health
WHERE completeness IS NOT NULL
  [[AND source = {{source}}]]
  [[AND started_at >= {{date_from}}]]
ORDER BY started_at""",
        "viz": {"graph.dimensions": ["started_at", "source"], "graph.metrics": ["completeness"],
                "graph.x_axis.title_text": "Запуск", "graph.y_axis.title_text": "Полнота"},
    },
}

# Дашборды: фильтры и карточки (ключ карточки, ширина, высота); сетка Metabase — 24 колонки
DASHBOARDS = [
    {
        "name": "Рынок",
        "description": "Предложение и цены по странам: активные, новые и снятые объявления, медианная цена в EUR.",
        "parameters": ["country", "date_from", "date_to"],
        "cards": [("market_countries", 24, 5), ("market_active", 12, 7), ("market_price", 12, 7),
                  ("market_flow", 12, 7), ("market_top_makes", 12, 7)],
    },
    {
        "name": "Сегмент",
        "description": "Марка, модель, годы и пробег: цена по неделям (например, Toyota Corolla 2019–2021 "
                       "в PL и DE), предложение, амортизация, срок экспозиции и снижения цены.",
        "parameters": ["make", "model", "country", "year_from", "year_to", "mileage_from", "mileage_to",
                       "date_from", "date_to"],
        "cards": [("segment_price_trend", 24, 8), ("segment_supply", 12, 7), ("segment_depreciation", 12, 7),
                  ("segment_delisted", 24, 5)],
    },
    {
        "name": "Объявление",
        "description": "Карточка объявления, цена по дням и журнал изменений. Укажите ID объявления на площадке.",
        "parameters": ["source", "listing"],
        "cards": [("listing_card", 24, 5), ("listing_price_history", 12, 7), ("listing_events", 12, 7)],
    },
    {
        "name": "Здоровье сбора",
        "description": "Запуски обхода: полнота, неполные шарды, снятия, предупреждения.",
        "parameters": ["source", "date_from"],
        "cards": [("health_runs", 24, 8), ("health_completeness", 24, 6)],
    },
]
