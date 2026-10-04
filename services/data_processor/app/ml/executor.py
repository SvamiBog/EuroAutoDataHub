"""Все вычисления LightGBM — в одном выделенном потоке.

Обучение и прогнозы не должны останавливать приём сообщений, поэтому выполняются вне event loop. LightGBM
параллелится через OpenMP, а OpenMP заводит свою команду потоков на каждый вызывающий поток: при
asyncio.to_thread (разные потоки пула) команды множатся и простаивающие потоки съедают процессор — обучение
шло в 4 раза медленнее. Один поток — одна команда OpenMP, переиспользуемая между вызовами.
"""
import asyncio
import functools
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml")


def threads(value: int) -> int:
    """Потоков LightGBM: заданное число или половина ядер. Все ядра брать нельзя: OpenMP ждёт на барьерах
    самый медленный поток, и любой другой активный поток процесса (приём из Kafka, драйвер БД) замедляет
    обучение в разы."""
    return value if value > 0 else max(1, (os.cpu_count() or 2) // 2)


async def run_ml(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    try:
        return await loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
    finally:
        logger.debug(f"ML {getattr(fn, '__name__', fn)}: {time.monotonic() - started:.1f} с")
