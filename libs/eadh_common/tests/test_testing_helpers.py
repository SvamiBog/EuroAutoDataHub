"""Временная БД с миграциями для pg-тестов."""
import pytest
import sqlalchemy as sa

from eadh_common.testing import migrated_database, get_test_database_url, truncate_all

pytestmark = pytest.mark.pg


@pytest.mark.skipif(not get_test_database_url(), reason="TEST_DATABASE_URL не задан")
def test_migrated_database_lifecycle():
    with migrated_database() as url:
        sync = sa.make_url(url).set(drivername="postgresql+psycopg2")
        engine = sa.create_engine(sync)
        with engine.begin() as connection:
            tables = set(connection.execute(sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
            assert {"listing", "crawl_run", "alembic_version"} <= tables
            connection.execute(sa.text("INSERT INTO vehicle_make (slug, name) VALUES ('audi', 'Audi')"))
        truncate_all(url)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM vehicle_make")).scalar() == 0
        engine.dispose()
    # после выхода БД удалена
    admin = sa.create_engine(sa.make_url(get_test_database_url()).set(drivername="postgresql+psycopg2"))
    with admin.connect() as connection:
        name = sa.make_url(url).database
        assert connection.execute(sa.text("SELECT count(*) FROM pg_database WHERE datname=:n"), {"n": name}).scalar() == 0
    admin.dispose()
