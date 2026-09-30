# services/api_service/app/crud/analytics.py
"""Аналитические запросы (PostgreSQL: percentile_cont, date_trunc, DISTINCT ON).

Цены — в EUR. В трендах и амортизации каждое объявление учитывается один раз за период
(по последнему наблюдению периода), чтобы долго висящие объявления не перевешивали.
Объявления с нарушениями качества данных (listing.quality_flags) не учитываются.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import Date, and_, case, cast, exists, extract, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from eadh_common.models import (
    DailyObservation, Listing, ListingEvent, SegmentDailyStats, VehicleMake, VehicleModel,
)
from eadh_common.normalize import slugify

PERIODS = ("day", "week", "month")
LEVELS = ("country", "make", "model", "model_year")


@dataclass
class ListingFilter:
    """Фильтры по характеристикам объявления (для трендов, амортизации, снятых)."""
    make: Optional[str] = None
    model: Optional[str] = None
    countries: Sequence[str] = ()
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    fuel_type: Optional[str] = None
    gearbox: Optional[str] = None
    # только объявления без нарушений качества данных (неправдоподобные цены, годы, пробег)
    quality_ok: bool = True

    def conditions(self) -> list:
        conditions = [Listing.quality_flags.is_(None)] if self.quality_ok else []
        if self.make:
            conditions.append(Listing.make_id.in_(select(VehicleMake.id).where(VehicleMake.slug == slugify(self.make))))
        if self.model:
            model_query = select(VehicleModel.id).where(VehicleModel.slug == slugify(self.model))
            if self.make:
                model_query = model_query.join(VehicleMake, VehicleMake.id == VehicleModel.make_id).where(
                    VehicleMake.slug == slugify(self.make))
            conditions.append(Listing.model_id.in_(model_query))
        if self.countries:
            conditions.append(Listing.country_code.in_([c.upper() for c in self.countries]))
        if self.year_from:
            conditions.append(Listing.year >= self.year_from)
        if self.year_to:
            conditions.append(Listing.year <= self.year_to)
        if self.fuel_type:
            conditions.append(Listing.fuel_type == self.fuel_type)
        if self.gearbox:
            conditions.append(Listing.gearbox == self.gearbox)
        return conditions


def _quantiles(column) -> list:
    return [
        func.percentile_cont(0.25).within_group(column.asc()).label("p25"),
        func.percentile_cont(0.5).within_group(column.asc()).label("median"),
        func.percentile_cont(0.75).within_group(column.asc()).label("p75"),
    ]


def _money(value) -> Optional[float]:
    return round(float(value), 2) if value is not None else None


async def price_trend(session: AsyncSession, filters: ListingFilter, date_from: date, date_to: date,
                      period: str = "week", mileage_from: Optional[int] = None,
                      mileage_to: Optional[int] = None) -> list[dict[str, Any]]:
    """Медиана и квартильный размах цены (EUR) по периодам и странам."""
    if period not in PERIODS:
        raise ValueError(f"Неизвестный период {period}")
    # литерал, а не параметр: иначе выражения в DISTINCT ON и ORDER BY не совпадут
    truncated = func.date_trunc(literal_column(f"'{period}'"), DailyObservation.obs_date)
    bucket = cast(truncated, Date).label("period_start")
    conditions = [
        DailyObservation.obs_date >= date_from, DailyObservation.obs_date <= date_to,
        DailyObservation.price_eur.is_not(None), *filters.conditions(),
    ]
    if mileage_from is not None:
        conditions.append(DailyObservation.mileage_km >= mileage_from)
    if mileage_to is not None:
        conditions.append(DailyObservation.mileage_km <= mileage_to)

    # последнее наблюдение каждого объявления в периоде
    per_listing = (
        select(bucket, DailyObservation.listing_id, DailyObservation.price_eur, Listing.country_code)
        .join(Listing, Listing.id == DailyObservation.listing_id)
        .where(*conditions)
        .distinct(truncated, DailyObservation.listing_id)
        .order_by(truncated, DailyObservation.listing_id, DailyObservation.obs_date.desc())
        .subquery()
    )
    query = (
        select(per_listing.c.period_start, per_listing.c.country_code,
               func.count().label("listings"), *_quantiles(per_listing.c.price_eur))
        .group_by(per_listing.c.period_start, per_listing.c.country_code)
        .order_by(per_listing.c.period_start, per_listing.c.country_code)
    )
    rows = (await session.execute(query)).all()
    return [{"period_start": row.period_start, "country_code": row.country_code, "listings": row.listings,
             "p25": _money(row.p25), "median": _money(row.median), "p75": _money(row.p75)} for row in rows]


def _segment_columns():
    make, model = aliased(VehicleMake), aliased(VehicleModel)
    columns = [SegmentDailyStats, make.slug.label("make_slug"), make.name.label("make_name"),
               model.slug.label("model_slug"), model.name.label("model_name")]
    return columns, make, model


def _segment_row(row) -> dict[str, Any]:
    stats: SegmentDailyStats = row[0]
    return {
        "stat_date": stats.stat_date, "segment_key": stats.segment_key, "level": stats.level,
        "country_code": stats.country_code, "make": row.make_slug, "make_name": row.make_name,
        "model": row.model_slug, "model_name": row.model_name, "year": stats.year,
        "active_count": stats.active_count, "new_count": stats.new_count,
        "delisted_count": stats.delisted_count, "observed_count": stats.observed_count,
        "price_eur_p25": _money(stats.price_eur_p25), "price_eur_median": _money(stats.price_eur_median),
        "price_eur_p75": _money(stats.price_eur_p75), "mileage_median": stats.mileage_median,
        "dom_median_days": float(stats.dom_median_days) if stats.dom_median_days is not None else None,
        "price_drop_count": stats.price_drop_count,
    }


def _segment_conditions(level: str, country: Optional[str], make: Optional[str], model: Optional[str],
                        year: Optional[int], make_alias, model_alias) -> list:
    conditions = [SegmentDailyStats.level == level]
    if country:
        conditions.append(SegmentDailyStats.country_code == country.upper())
    if make:
        conditions.append(make_alias.slug == slugify(make))
    if model:
        conditions.append(model_alias.slug == slugify(model))
    if year is not None:
        conditions.append(SegmentDailyStats.year == year)
    return conditions


async def latest_stat_date(session: AsyncSession) -> Optional[date]:
    return (await session.execute(select(func.max(SegmentDailyStats.stat_date)))).scalar_one_or_none()


async def segments(session: AsyncSession, level: str, stat_date: Optional[date] = None,
                   country: Optional[str] = None, make: Optional[str] = None, model: Optional[str] = None,
                   min_observed: int = 0, limit: int = 50) -> tuple[Optional[date], list[dict[str, Any]]]:
    """Сегменты уровня level за дату (по умолчанию — последнюю посчитанную), крупные первыми."""
    stat_date = stat_date or await latest_stat_date(session)
    if stat_date is None:
        return None, []
    columns, make_alias, model_alias = _segment_columns()
    query = (
        select(*columns)
        .outerjoin(make_alias, make_alias.id == SegmentDailyStats.make_id)
        .outerjoin(model_alias, model_alias.id == SegmentDailyStats.model_id)
        .where(SegmentDailyStats.stat_date == stat_date, SegmentDailyStats.observed_count >= min_observed,
               *_segment_conditions(level, country, make, model, None, make_alias, model_alias))
        .order_by(SegmentDailyStats.active_count.desc(), SegmentDailyStats.segment_key)
        .limit(limit)
    )
    return stat_date, [_segment_row(row) for row in (await session.execute(query)).all()]


async def segment_timeseries(session: AsyncSession, level: str, date_from: date, date_to: date,
                             country: Optional[str] = None, make: Optional[str] = None,
                             model: Optional[str] = None, year: Optional[int] = None) -> list[dict[str, Any]]:
    """Дневной ряд витрины для сегментов, подходящих под фильтры."""
    columns, make_alias, model_alias = _segment_columns()
    query = (
        select(*columns)
        .outerjoin(make_alias, make_alias.id == SegmentDailyStats.make_id)
        .outerjoin(model_alias, model_alias.id == SegmentDailyStats.model_id)
        .where(SegmentDailyStats.stat_date >= date_from, SegmentDailyStats.stat_date <= date_to,
               *_segment_conditions(level, country, make, model, year, make_alias, model_alias))
        .order_by(SegmentDailyStats.segment_key, SegmentDailyStats.stat_date)
    )
    return [_segment_row(row) for row in (await session.execute(query)).all()]


async def depreciation(session: AsyncSession, filters: ListingFilter, date_from: date,
                       date_to: date, max_age: int = 25) -> list[dict[str, Any]]:
    """Кривая амортизации: цена (EUR) в зависимости от возраста автомобиля."""
    age = (extract("year", DailyObservation.obs_date) - Listing.year).label("age")
    per_listing = (
        select(age, Listing.country_code, DailyObservation.price_eur, DailyObservation.mileage_km)
        .join(Listing, Listing.id == DailyObservation.listing_id)
        .where(DailyObservation.obs_date >= date_from, DailyObservation.obs_date <= date_to,
               DailyObservation.price_eur.is_not(None), Listing.year.is_not(None), *filters.conditions())
        .distinct(DailyObservation.listing_id)
        .order_by(DailyObservation.listing_id, DailyObservation.obs_date.desc())
        .subquery()
    )
    query = (
        select(per_listing.c.age, per_listing.c.country_code, func.count().label("listings"),
               *_quantiles(per_listing.c.price_eur),
               func.percentile_cont(0.5).within_group(per_listing.c.mileage_km.asc()).label("mileage_median"))
        .where(per_listing.c.age >= 0, per_listing.c.age <= max_age)
        .group_by(per_listing.c.age, per_listing.c.country_code)
        .order_by(per_listing.c.country_code, per_listing.c.age)
    )
    rows = (await session.execute(query)).all()
    return [{"age_years": int(row.age), "country_code": row.country_code, "listings": row.listings,
             "p25": _money(row.p25), "median": _money(row.median), "p75": _money(row.p75),
             "mileage_median": int(row.mileage_median) if row.mileage_median is not None else None}
            for row in rows]


async def delisted_summary(session: AsyncSession, filters: ListingFilter, date_from: date,
                           date_to: date) -> list[dict[str, Any]]:
    """Снятые за период: срок экспозиции, доля со снижением цены, медианная скидка от первой цены."""
    start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    first_price = (
        select(ListingEvent.price)
        .where(ListingEvent.listing_id == Listing.id, ListingEvent.event_type == "new")
        .order_by(ListingEvent.ts).limit(1).scalar_subquery()
    )
    had_drop = exists().where(ListingEvent.listing_id == Listing.id, ListingEvent.event_type == "price_change",
                              ListingEvent.price < ListingEvent.old_price)
    delisted = (
        select(Listing.country_code,
               (extract("epoch", Listing.last_seen_at - Listing.first_seen_at) / 86400.0).label("dom_days"),
               first_price.label("first_price"), Listing.price.label("last_price"), had_drop.label("had_drop"))
        .where(Listing.status == "delisted", Listing.delisted_at >= start, Listing.delisted_at < end,
               *filters.conditions())
        .subquery()
    )
    discount = case(
        (and_(delisted.c.had_drop, delisted.c.first_price > 0),
         (delisted.c.first_price - delisted.c.last_price) / delisted.c.first_price),
    )
    query = (
        select(delisted.c.country_code, func.count().label("delisted"),
               func.count().filter(delisted.c.had_drop).label("with_price_drop"),
               func.percentile_cont(0.5).within_group(delisted.c.dom_days.asc()).label("dom_median"),
               func.percentile_cont(0.5).within_group(discount.asc()).label("discount_median"))
        .group_by(delisted.c.country_code)
        .order_by(delisted.c.country_code)
    )
    rows = (await session.execute(query)).all()
    return [{"country_code": row.country_code, "delisted": row.delisted,
             "with_price_drop": row.with_price_drop,
             "price_drop_share": round(row.with_price_drop / row.delisted, 4) if row.delisted else None,
             "dom_median_days": round(float(row.dom_median), 1) if row.dom_median is not None else None,
             "discount_median": round(float(row.discount_median), 4) if row.discount_median is not None else None}
            for row in rows]
