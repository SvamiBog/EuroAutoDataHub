"""Аномалии из командной строки.

  python -m app.anomalies detect --from 2026-09-01 --to 2026-09-30   # поведенческие и рыночные за период
  python -m app.anomalies detect --snapshot                           # + справедливые цены и дайджесты (сегодня)
  python -m app.anomalies sample --rule price_below_market -n 100 --out sample.csv
  python -m app.anomalies labels sample.csv
  python -m app.anomalies precision
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from app.aggregates import dates_between


async def main(args) -> None:
    from app.anomalies.labels import export_sample, import_labels, precision_by_rule
    from app.anomalies.runner import detect_day, refresh_quality
    from app.core.config import settings
    from app.db_session import engine, session_factory

    try:
        if args.command == "detect":
            now = datetime.now(timezone.utc)
            last = args.last or args.first
            if args.snapshot:
                await refresh_quality(session_factory, settings, last)
            for day in dates_between(args.first, last):
                await detect_day(session_factory, settings, day, now, snapshot=args.snapshot and day == last)
        elif args.command == "sample":
            async with session_factory() as session:
                count = await export_sample(session, args.rule, args.n, Path(args.out))
            print(f"Выгружено {count} аномалий в {args.out}; заполните колонку label (1/0)")
        elif args.command == "labels":
            async with session_factory() as session:
                counts = await import_labels(session, Path(args.path))
                await session.commit()
            print(f"Разметка загружена: {counts}")
        elif args.command == "precision":
            async with session_factory() as session:
                print(json.dumps(await precision_by_rule(session), ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    parser = argparse.ArgumentParser(description="Аномалии: детекция, выборка для разметки, precision")
    commands = parser.add_subparsers(dest="command", required=True)
    detect = commands.add_parser("detect", help="запустить детекторы за период")
    today = datetime.now(timezone.utc).date()
    detect.add_argument("--from", dest="first", type=date.fromisoformat, default=today)
    detect.add_argument("--to", dest="last", type=date.fromisoformat, default=None)
    detect.add_argument("--snapshot", action="store_true",
                        help="для последнего дня пересчитать справедливые цены и отправить дайджесты")
    sample = commands.add_parser("sample", help="случайная выборка неразмеченных аномалий в CSV")
    sample.add_argument("--rule", default="price_below_market")
    sample.add_argument("-n", type=int, default=100)
    sample.add_argument("--out", default="anomaly_sample.csv")
    labels = commands.add_parser("labels", help="загрузить разметку из CSV")
    labels.add_argument("path")
    commands.add_parser("precision", help="precision по правилам (по разметке)")
    asyncio.run(main(parser.parse_args()))
