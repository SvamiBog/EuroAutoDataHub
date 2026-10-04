"""ML из командной строки.

  python -m app.ml train [price|dom|all] [--no-activate]   # обучить сейчас (проверка качества — как по расписанию)
  python -m app.ml status                                   # версии моделей и метрики
  python -m app.ml activate price price-20261004-021500     # сделать версию активной (откат)
  python -m app.ml refresh                                  # пересчитать справедливые цены, прогноз срока, арбитраж
  python -m app.ml benchmark [-n 12000] [--seed 11]         # v1 против модели на синтетическом рынке
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone


async def main(args) -> None:
    from app.core.config import settings

    if args.command == "benchmark":
        from app.ml.benchmark import benchmark

        print(json.dumps(benchmark(settings, n=args.n, seed=args.seed), ensure_ascii=False, indent=2, default=str))
        return

    from app.anomalies.prices import detect_price_anomalies
    from app.db_session import engine, session_factory
    from app.ml import registry
    from app.ml.service import refresh_arbitrage, refresh_dom_forecasts, train_dom, train_price

    now = datetime.now(timezone.utc)
    try:
        if args.command == "train":
            kinds = ["price", "dom"] if args.kind == "all" else [args.kind]
            for kind in kinds:
                train = train_price if kind == "price" else train_dom
                result = await train(session_factory, settings, now, activate=not args.no_activate)
                print(json.dumps({kind: result}, ensure_ascii=False, indent=2, default=str))
        elif args.command == "status":
            async with session_factory() as session:
                for record in await registry.list_models(session):
                    model = record.metrics.get("model", {})
                    print(f"{record.version:28} {record.status:9} обучение {record.n_train:>7} проверка "
                          f"{record.n_valid:>6}  " + (f"MAPE {model.get('mape', 0):.1%} покрытие "
                                                       f"{model.get('coverage', 0):.0%}" if model else
                                                       json.dumps(record.metrics.get("horizons", {}),
                                                                  ensure_ascii=False))
                          + (f"  [{record.note}]" if record.note else ""))
        elif args.command == "activate":
            async with session_factory() as session:
                record = await registry.get_model(session, args.kind, args.version)
                if record is None:
                    sys.exit(f"Нет модели {args.kind} {args.version}")
                await registry.activate(session, record, now)
                await session.commit()
            print(f"Активна {args.kind} {args.version}")
        elif args.command == "refresh":
            async with session_factory() as session:
                prices = await detect_price_anomalies(session, settings, now.date(), now)
                await session.commit()
            prices.pop("created")
            dom = await refresh_dom_forecasts(session_factory, settings, now)
            arbitrage = await refresh_arbitrage(session_factory, settings, now)
            print(json.dumps({"prices": prices, "dom": dom, "arbitrage": arbitrage}, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    parser = argparse.ArgumentParser(description="ML: модели справедливой цены и срока до снятия, арбитраж")
    commands = parser.add_subparsers(dest="command", required=True)
    train = commands.add_parser("train", help="обучить модель сейчас")
    train.add_argument("kind", nargs="?", choices=["price", "dom", "all"], default="all")
    train.add_argument("--no-activate", action="store_true", help="не делать активной, даже если прошла проверку")
    commands.add_parser("status", help="версии моделей и метрики")
    activate = commands.add_parser("activate", help="сделать версию активной")
    activate.add_argument("kind", choices=["price", "dom"])
    activate.add_argument("version")
    commands.add_parser("refresh", help="пересчитать оценки, прогноз срока и арбитраж")
    bench = commands.add_parser("benchmark", help="v1 против модели на синтетическом рынке")
    bench.add_argument("-n", type=int, default=12000)
    bench.add_argument("--seed", type=int, default=11)
    asyncio.run(main(parser.parse_args()))
