"""Модель данных v2: listing, listing_event, crawl_run/crawl_shard, справочники, курсы валют

Переносит данные из auto_ad / auto_ad_history / car_make / car_model. Старые таблицы
не удаляются, а переименовываются в legacy_* (для отката и сверки).

Revision ID: 7b1e4c2d9a10
Revises: f6c594b769f2
Create Date: 2026-09-30 08:46:50.404564

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7b1e4c2d9a10'
down_revision: Union[str, None] = 'f6c594b769f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_TABLES = ("auto_ad_history", "auto_ad", "car_model", "car_make")

# Та же нормализация, что eadh_common.normalize.slugify: нижний регистр, пробелы и '_' -> '-'
def _slug(expr: str) -> str:
    return f"regexp_replace(lower(trim({expr})), '[[:space:]_]+', '-', 'g')"


def _migrate_legacy_data() -> None:
    """Перенос данных из таблиц модели v1 (уже переименованных в legacy_*)."""
    source = "coalesce(a.source_name, 'otomoto.pl')"

    # --- Справочник марок: из car_make и из самих объявлений ---
    op.execute(f"""
        INSERT INTO vehicle_make (slug, name)
        SELECT DISTINCT ON ({_slug('c.slug')}) {_slug('c.slug')}, c.name
        FROM legacy_car_make c WHERE trim(c.slug) <> ''
        ORDER BY {_slug('c.slug')}, c.id
        ON CONFLICT (slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO vehicle_make (slug, name)
        SELECT DISTINCT {_slug('a.make_name')}, lower(trim(a.make_name))
        FROM legacy_auto_ad a WHERE trim(coalesce(a.make_name, '')) <> ''
        ON CONFLICT (slug) DO NOTHING
    """)

    # --- Справочник моделей: slug модели внутри марки (в v1 slug был "марка-модель") ---
    op.execute(f"""
        INSERT INTO vehicle_model (make_id, slug, name)
        SELECT DISTINCT vm.id, {_slug('cm.name')}, cm.name
        FROM legacy_car_model cm
        JOIN legacy_car_make c ON c.id = cm.make_id
        JOIN vehicle_make vm ON vm.slug = {_slug('c.slug')}
        WHERE trim(cm.name) <> ''
        ON CONFLICT (make_id, slug) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO vehicle_model (make_id, slug, name)
        SELECT DISTINCT vm.id, {_slug('a.model_name')}, lower(trim(a.model_name))
        FROM legacy_auto_ad a
        JOIN vehicle_make vm ON vm.slug = {_slug('a.make_name')}
        WHERE trim(coalesce(a.model_name, '')) <> ''
        ON CONFLICT (make_id, slug) DO NOTHING
    """)

    # --- Алиасы значений площадки ---
    op.execute(f"""
        INSERT INTO make_alias (source, raw_value, make_id)
        SELECT DISTINCT {source}, lower(trim(a.make_name)), vm.id
        FROM legacy_auto_ad a JOIN vehicle_make vm ON vm.slug = {_slug('a.make_name')}
        ON CONFLICT (source, raw_value) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO model_alias (source, make_id, raw_value, model_id)
        SELECT DISTINCT {source}, vm.id, lower(trim(a.model_name)), vmo.id
        FROM legacy_auto_ad a
        JOIN vehicle_make vm ON vm.slug = {_slug('a.make_name')}
        JOIN vehicle_model vmo ON vmo.make_id = vm.id AND vmo.slug = {_slug('a.model_name')}
        ON CONFLICT (source, make_id, raw_value) DO NOTHING
    """)

    # --- Объявления. Колонки v1 без часового пояса хранят UTC ---
    op.execute(f"""
        INSERT INTO listing (
            source, source_listing_id, country_code, url, title,
            make_raw, model_raw, version_raw, generation_raw, make_id, model_id,
            year, mileage_km, fuel_type, gearbox, transmission, color,
            engine_capacity_cm3, engine_power_hp, region, city, seller_ref,
            price, currency, posted_at, first_seen_at, last_seen_at,
            status, delisted_at, missed_complete_runs
        )
        SELECT
            {source}, a.id_ad, CASE WHEN {source} = 'otomoto.pl' THEN 'PL' ELSE 'XX' END, a.url_ad, a.title,
            lower(trim(a.make_name)), lower(trim(a.model_name)), a.version, a.generation, vm.id, vmo.id,
            a.year, a.mileage, a.fuel_type, a.gearbox, a.transmission, a.color,
            a.engine_capacity, a.engine_power, a.region, a.city, a."sellerLink",
            a.price, a."currencyCode",
            a."createdAt" AT TIME ZONE 'UTC',
            least(a."createdAt", coalesce(h.first_ts, a."createdAt")) AT TIME ZONE 'UTC',
            coalesce(a.sold_at, greatest(a."createdAt", coalesce(h.last_ts, a."createdAt"))) AT TIME ZONE 'UTC',
            CASE WHEN a.sold_at IS NULL THEN 'active' ELSE 'delisted' END,
            a.sold_at AT TIME ZONE 'UTC',
            0
        FROM legacy_auto_ad a
        LEFT JOIN vehicle_make vm ON vm.slug = {_slug('a.make_name')}
        LEFT JOIN vehicle_model vmo ON vmo.make_id = vm.id AND vmo.slug = {_slug('a.model_name')}
        LEFT JOIN (
            SELECT auto_ad_id, min("timestamp") AS first_ts, max("timestamp") AS last_ts
            FROM legacy_auto_ad_history GROUP BY auto_ad_id
        ) h ON h.auto_ad_id = a.id_ad
    """)

    # --- Журнал изменений ---
    op.execute(f"""
        INSERT INTO listing_event (listing_id, event_type, ts, price, old_price, currency)
        SELECT l.id,
               CASE h.status
                   WHEN 'active' THEN 'new'
                   WHEN 'price_changed' THEN 'price_change'
                   WHEN 'sold' THEN 'delisted'
                   ELSE h.status
               END,
               h."timestamp" AT TIME ZONE 'UTC',
               h.price,
               CASE WHEN h.status = 'price_changed'
                    THEN lag(h.price) OVER (PARTITION BY h.auto_ad_id ORDER BY h."timestamp", h.id) END,
               h."currencyCode"
        FROM legacy_auto_ad_history h
        JOIN legacy_auto_ad a ON a.id_ad = h.auto_ad_id
        JOIN listing l ON l.source = {source} AND l.source_listing_id = a.id_ad
        ORDER BY h.id
    """)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('crawl_run',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=36), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('finish_reason', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('shards_planned', sa.Integer(), nullable=True),
    sa.Column('stats', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('report', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('report_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_crawl_run_source'), 'crawl_run', ['source'], unique=False)
    op.create_table('fx_rate',
    sa.Column('rate_date', sa.Date(), nullable=False),
    sa.Column('currency', sqlmodel.sql.sqltypes.AutoString(length=3), nullable=False),
    sa.Column('rate_per_eur', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.PrimaryKeyConstraint('rate_date', 'currency')
    )
    op.create_table('vehicle_make',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_vehicle_make_slug'), 'vehicle_make', ['slug'], unique=True)
    op.create_table('crawl_shard',
    sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(length=36), nullable=False),
    sa.Column('shard_key', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('filters', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expected_count', sa.Integer(), nullable=False),
    sa.Column('collected_count', sa.Integer(), nullable=False),
    sa.Column('pages_total', sa.Integer(), nullable=False),
    sa.Column('pages_failed', sa.Integer(), nullable=False),
    sa.Column('complete', sa.Boolean(), nullable=False),
    sa.Column('lifecycle_status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('lifecycle_applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('missed_count', sa.Integer(), nullable=True),
    sa.Column('delisted_count', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['crawl_run.id'], ),
    sa.PrimaryKeyConstraint('run_id', 'shard_key')
    )
    op.create_index('ix_crawl_shard_lifecycle', 'crawl_shard', ['lifecycle_status', 'finished_at'], unique=False)
    op.create_index(op.f('ix_crawl_shard_source'), 'crawl_shard', ['source'], unique=False)
    op.create_table('make_alias',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('raw_value', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('make_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['make_id'], ['vehicle_make.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'raw_value', name='uq_make_alias')
    )
    op.create_index(op.f('ix_make_alias_make_id'), 'make_alias', ['make_id'], unique=False)
    op.create_table('vehicle_model',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('make_id', sa.Integer(), nullable=False),
    sa.Column('slug', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.ForeignKeyConstraint(['make_id'], ['vehicle_make.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('make_id', 'slug', name='uq_vehicle_model_make_slug')
    )
    op.create_index(op.f('ix_vehicle_model_make_id'), 'vehicle_model', ['make_id'], unique=False)
    op.create_table('listing',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('source_listing_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('country_code', sqlmodel.sql.sqltypes.AutoString(length=2), nullable=False),
    sa.Column('url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('make_raw', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('model_raw', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('version_raw', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('generation_raw', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('make_id', sa.Integer(), nullable=True),
    sa.Column('model_id', sa.Integer(), nullable=True),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('mileage_km', sa.Integer(), nullable=True),
    sa.Column('fuel_type', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('gearbox', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('transmission', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('color', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('engine_capacity_cm3', sa.Integer(), nullable=True),
    sa.Column('engine_power_hp', sa.Integer(), nullable=True),
    sa.Column('vin', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
    sa.Column('region', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('city', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('seller_ref', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('image_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('currency', sqlmodel.sql.sqltypes.AutoString(length=3), nullable=True),
    sa.Column('price_eur', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_run_id', sqlmodel.sql.sqltypes.AutoString(length=36), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('delisted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('missed_complete_runs', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['make_id'], ['vehicle_make.id'], ),
    sa.ForeignKeyConstraint(['model_id'], ['vehicle_model.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'source_listing_id', name='uq_listing_source_id')
    )
    op.create_index(op.f('ix_listing_delisted_at'), 'listing', ['delisted_at'], unique=False)
    op.create_index(op.f('ix_listing_first_seen_at'), 'listing', ['first_seen_at'], unique=False)
    op.create_index(op.f('ix_listing_last_seen_run_id'), 'listing', ['last_seen_run_id'], unique=False)
    op.create_index(op.f('ix_listing_make_id'), 'listing', ['make_id'], unique=False)
    op.create_index(op.f('ix_listing_model_id'), 'listing', ['model_id'], unique=False)
    op.create_index('ix_listing_scope', 'listing', ['source', 'status', 'make_raw', 'model_raw', 'year'], unique=False)
    op.create_index(op.f('ix_listing_vin'), 'listing', ['vin'], unique=False)
    op.create_table('model_alias',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('make_id', sa.Integer(), nullable=False),
    sa.Column('raw_value', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('model_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['make_id'], ['vehicle_make.id'], ),
    sa.ForeignKeyConstraint(['model_id'], ['vehicle_model.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'make_id', 'raw_value', name='uq_model_alias')
    )
    op.create_index(op.f('ix_model_alias_model_id'), 'model_alias', ['model_id'], unique=False)
    op.create_table('listing_event',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('listing_id', sa.BigInteger(), nullable=False),
    sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('run_id', sqlmodel.sql.sqltypes.AutoString(length=36), nullable=True),
    sa.Column('price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('old_price', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('currency', sqlmodel.sql.sqltypes.AutoString(length=3), nullable=True),
    sa.Column('mileage_km', sa.Integer(), nullable=True),
    sa.Column('old_mileage_km', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['listing_id'], ['listing.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_listing_event_listing_id'), 'listing_event', ['listing_id'], unique=False)
    op.create_index(op.f('ix_listing_event_run_id'), 'listing_event', ['run_id'], unique=False)
    op.create_index(op.f('ix_listing_event_ts'), 'listing_event', ['ts'], unique=False)

    # Таблицы модели v1 сохраняем под префиксом legacy_
    for table in LEGACY_TABLES:
        op.rename_table(table, f"legacy_{table}")

    _migrate_legacy_data()


def downgrade() -> None:
    """Downgrade schema: удаляем таблицы v2 и возвращаем таблицы v1 (данные v1 не менялись)."""
    for table in ("listing_event", "listing", "model_alias", "make_alias", "vehicle_model",
                  "crawl_shard", "crawl_run", "fx_rate", "vehicle_make"):
        op.drop_table(table)
    for table in LEGACY_TABLES:
        op.rename_table(f"legacy_{table}", table)
