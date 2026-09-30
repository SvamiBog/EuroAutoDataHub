"""Помощники для тестов на настоящем PostgreSQL.

Тесты с маркером `pg` запускаются, только если задан TEST_DATABASE_URL, например
postgresql+asyncpg://postgres:postgres@localhost:5432/postgres. Для каждого запуска
создаётся временная БД, к ней применяются миграции Alembic (как в продакшене).
"""
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import sqlalchemy as sa

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
# Репозиторий: libs/eadh_common/src/eadh_common/testing.py -> корень на 4 уровня выше
ALEMBIC_INI = Path(__file__).resolve().parents[4] / "services" / "api_service" / "alembic.ini"


def get_test_database_url() -> Optional[str]:
    return os.getenv(TEST_DATABASE_URL_ENV)


def _sync(url: sa.URL, database: Optional[str] = None) -> sa.URL:
    return url.set(drivername="postgresql+psycopg2", database=database or url.database)


def apply_migrations(sync_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    previous = os.environ.get("SYNC_DATABASE_URL")
    os.environ["SYNC_DATABASE_URL"] = sync_url  # migrations/env.py берёт URL из окружения
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("SYNC_DATABASE_URL", None)
        else:
            os.environ["SYNC_DATABASE_URL"] = previous


@contextmanager
def migrated_database(base_url: Optional[str] = None) -> Iterator[str]:
    """Временная БД с применёнными миграциями. Возвращает async URL, после выхода БД удаляется."""
    base = sa.make_url(base_url or get_test_database_url())
    name = f"eadh_test_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(_sync(base), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        apply_migrations(_sync(base, name).render_as_string(hide_password=False))
        yield base.set(drivername="postgresql+asyncpg", database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def truncate_all(async_url: str) -> None:
    """Очищает все таблицы данных (схема и alembic_version остаются)."""
    url = sa.make_url(async_url)
    engine = sa.create_engine(_sync(url))
    with engine.begin() as connection:
        tables = connection.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version' AND tablename NOT LIKE 'legacy_%'")).scalars().all()
        # партиции очищаются через родительскую таблицу
        parents = [t for t in tables if not t.startswith("listing_observation_")]
        if parents:
            connection.execute(sa.text(
                "TRUNCATE " + ", ".join(f'"{t}"' for t in parents) + " RESTART IDENTITY CASCADE"))
    engine.dispose()
