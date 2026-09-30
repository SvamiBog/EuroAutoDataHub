"""Отчёт по пробному обходу (make probe): по JSONL из OUTPUT_FILE проверяет, что паук работает на живом сайте.

Проверки: есть полный шард с объявлениями, основные поля заполнены, нет ошибок API и неприменённых фильтров.
Код выхода 1 — что-то не так (подробности в выводе).

Запуск: python -m car_scrapers.probe probe_autovit.jsonl
"""
import json
import sys
from collections import Counter
from pathlib import Path

REQUIRED_FIELDS = ("price", "make", "model", "year", "mileage_km")
MIN_FILL_RATE = 0.9


def load(path: Path) -> tuple[list[dict], list[dict]]:
    items, events = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        message = json.loads(line)
        (events if message["topic"] == "crawl_events" else items).append(message["value"])
    return items, events


def check(items: list[dict], events: list[dict]) -> tuple[list[str], list[str]]:
    """(строки отчёта, найденные проблемы)."""
    lines, problems = [], []
    shards = [e for e in events if e.get("event") == "shard_finished"]
    finished = next((e for e in events if e.get("event") == "run_finished"), None)
    stats = (finished or {}).get("stats") or {}

    lines.append(f"Объявлений: {len(items)}; шардов: {len(shards)}; "
                 f"завершение: {(finished or {}).get('finish_reason', 'нет run_finished')}")
    for shard in shards:
        lines.append(f"  {'✅' if shard['complete'] else '⚠️'} {shard['shard_key']}: "
                     f"{shard['collected_count']} из {shard['expected_count']}, страниц {shard['pages_total']}")
    if not any(s["complete"] and s["collected_count"] for s in shards):
        problems.append("нет ни одного полного шарда с объявлениями")

    if items:
        for field in REQUIRED_FIELDS:
            rate = sum(1 for item in items if item.get(field) not in (None, "")) / len(items)
            lines.append(f"  поле {field}: заполнено {rate:.0%}")
            if rate < MIN_FILL_RATE:
                problems.append(f"поле {field} заполнено у {rate:.0%} объявлений")
        countries = Counter(item.get("country_code") for item in items)
        lines.append(f"  страны: {dict(countries)}; валюты: {dict(Counter(i.get('currency') for i in items))}")
        for item in items[:3]:
            lines.append(f"  пример: {item.get('make')} {item.get('model')} {item.get('year')}, "
                         f"{item.get('mileage_km')} км, {item.get('price')} {item.get('currency')} — {item.get('url')}")
    else:
        problems.append("не собрано ни одного объявления")

    for key in ("graphql_errors", "http_errors", "json_decode_errors", "missing_data_errors", "filter_mismatch",
                "forbidden_403"):
        if stats.get(key):
            problems.append(f"{key}: {stats[key]}")
    return lines, problems


def main(path: str) -> int:
    items, events = load(Path(path))
    lines, problems = check(items, events)
    print("\n".join(lines))
    if problems:
        print("❌ Проблемы:\n" + "\n".join(f"  • {p}" for p in problems))
        return 1
    print("✅ Паук работает на живом сайте")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
