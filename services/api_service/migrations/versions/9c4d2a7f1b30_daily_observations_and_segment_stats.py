"""Наблюдения по дням (партиции по месяцам), витрина segment_daily_stats, представления для BI

Revision ID: 9c4d2a7f1b30
Revises: 7b1e4c2d9a10
Create Date: 2026-09-30 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel
import eadh_common.models  # noqa: F401  (пользовательские типы, например UTCDateTime)


# revision identifiers, used by Alembic.
revision: str = '9c4d2a7f1b30'
down_revision: Union[str, None] = '7b1e4c2d9a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VIEWS = ("v_crawl_health", "v_segment_stats", "v_listing")


def upgrade() -> None:
    """Upgrade schema."""
    # --- listing_observation: партиционирована по месяцам ---
    op.execute("""
        CREATE TABLE listing_observation (
            listing_id BIGINT NOT NULL REFERENCES listing (id) ON DELETE CASCADE,
            obs_date DATE NOT NULL,
            observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
            run_id VARCHAR(36),
            price NUMERIC(14, 2),
            currency VARCHAR(3),
            price_eur NUMERIC(14, 2),
            mileage_km INTEGER,
            PRIMARY KEY (listing_id, obs_date)
        ) PARTITION BY RANGE (obs_date)
    """)
    op.execute("CREATE INDEX ix_listing_observation_obs_date ON listing_observation (obs_date)")
    op.execute("""
        CREATE OR REPLACE FUNCTION ensure_listing_observation_partition(d date) RETURNS void AS $$
        DECLARE
            part_name text := 'listing_observation_' || to_char(d, 'YYYY_MM');
        BEGIN
            IF to_regclass(part_name) IS NULL THEN
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF listing_observation FOR VALUES FROM (%L) TO (%L)',
                    part_name, date_trunc('month', d)::date, (date_trunc('month', d) + interval '1 month')::date);
            END IF;
        END
        $$ LANGUAGE plpgsql
    """)
    # Партиции на текущий и следующий месяц и на даты уже накопленных данных
    op.execute("""
        SELECT ensure_listing_observation_partition(m::date)
        FROM generate_series(
            date_trunc('month', LEAST(current_date, COALESCE((SELECT min(last_seen_at AT TIME ZONE 'UTC') FROM listing), current_date))),
            date_trunc('month', current_date) + interval '1 month',
            interval '1 month') AS m
    """)
    # Последнее известное наблюдение каждого объявления — отправная точка истории цен
    op.execute("""
        INSERT INTO listing_observation (listing_id, obs_date, observed_at, run_id, price, currency, price_eur, mileage_km)
        SELECT id, (last_seen_at AT TIME ZONE 'UTC')::date, last_seen_at, last_seen_run_id,
               price, currency, price_eur, mileage_km
        FROM listing
    """)

    # --- segment_daily_stats ---
    op.create_table('segment_daily_stats',
    sa.Column('stat_date', sa.Date(), nullable=False),
    sa.Column('segment_key', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('level', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('country_code', sqlmodel.sql.sqltypes.AutoString(length=2), nullable=False),
    sa.Column('make_id', sa.Integer(), nullable=True),
    sa.Column('model_id', sa.Integer(), nullable=True),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('active_count', sa.Integer(), nullable=False),
    sa.Column('new_count', sa.Integer(), nullable=False),
    sa.Column('delisted_count', sa.Integer(), nullable=False),
    sa.Column('observed_count', sa.Integer(), nullable=False),
    sa.Column('price_eur_p25', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('price_eur_median', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('price_eur_p75', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('mileage_median', sa.Integer(), nullable=True),
    sa.Column('dom_median_days', sa.Numeric(precision=8, scale=1), nullable=True),
    sa.Column('price_drop_count', sa.Integer(), nullable=False),
    sa.Column('computed_at', eadh_common.models.UTCDateTime(), nullable=False),
    sa.PrimaryKeyConstraint('stat_date', 'segment_key')
    )
    op.create_index('ix_segment_stats_lookup', 'segment_daily_stats',
                    ['level', 'country_code', 'make_id', 'model_id', 'stat_date'], unique=False)

    # --- Представления для BI (Metabase) ---
    op.execute("""
        CREATE VIEW v_listing AS
        SELECT l.*,
               vm.slug AS make_slug, vm.name AS make_name,
               vmo.slug AS model_slug, vmo.name AS model_name,
               EXTRACT(DAY FROM (CASE WHEN l.status = 'delisted' THEN l.last_seen_at ELSE now() END)
                                - l.first_seen_at)::int AS days_on_market
        FROM listing l
        LEFT JOIN vehicle_make vm ON vm.id = l.make_id
        LEFT JOIN vehicle_model vmo ON vmo.id = l.model_id
    """)
    op.execute("""
        CREATE VIEW v_segment_stats AS
        SELECT s.*,
               vm.slug AS make_slug, vm.name AS make_name,
               vmo.slug AS model_slug, vmo.name AS model_name
        FROM segment_daily_stats s
        LEFT JOIN vehicle_make vm ON vm.id = s.make_id
        LEFT JOIN vehicle_model vmo ON vmo.id = s.model_id
    """)
    op.execute("""
        CREATE VIEW v_crawl_health AS
        SELECT r.id AS run_id, r.source, r.started_at, r.finished_at,
               round((EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) / 60)::numeric, 1) AS duration_min,
               r.finish_reason, r.shards_planned,
               count(s.shard_key) AS shards,
               count(s.shard_key) FILTER (WHERE s.complete) AS shards_complete,
               COALESCE(sum(s.expected_count), 0) AS expected,
               COALESCE(sum(s.collected_count), 0) AS collected,
               CASE WHEN sum(s.expected_count) > 0
                    THEN round(sum(s.collected_count)::numeric / sum(s.expected_count), 4) END AS completeness,
               COALESCE(sum(s.delisted_count), 0) AS delisted,
               (r.report -> 'events' ->> 'new')::int AS new_listings,
               jsonb_array_length(COALESCE(r.report -> 'warnings', '[]'::jsonb)) AS warnings_count,
               r.report -> 'warnings' AS warnings
        FROM crawl_run r
        LEFT JOIN crawl_shard s ON s.run_id = r.id
        GROUP BY r.id
    """)


def downgrade() -> None:
    """Downgrade schema."""
    for view in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
    op.drop_index('ix_segment_stats_lookup', table_name='segment_daily_stats')
    op.drop_table('segment_daily_stats')
    op.execute("DROP TABLE IF EXISTS listing_observation CASCADE")  # вместе с партициями
    op.execute("DROP FUNCTION IF EXISTS ensure_listing_observation_partition(date)")
