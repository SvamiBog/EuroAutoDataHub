"""Реестр моделей (таблица ml_model): версии, метрики, активная модель каждого вида."""
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import select

from eadh_common.models import MlModel

ACTIVE, CANDIDATE, RETIRED = "active", "candidate", "retired"


def new_version(kind: str, now: datetime) -> str:
    return f"{kind}-{now:%Y%m%d-%H%M%S}"


async def save_model(session: AsyncSession, *, kind: str, version: str, trained_at: datetime, n_train: int,
                     n_valid: int, params: dict[str, Any], features: dict[str, Any], metrics: dict[str, Any],
                     artifact: dict[str, Any], train_from=None, valid_from=None, valid_to=None,
                     note: Optional[str] = None) -> MlModel:
    record = MlModel(kind=kind, version=version, status=CANDIDATE, trained_at=trained_at, train_from=train_from,
                     valid_from=valid_from, valid_to=valid_to, n_train=n_train, n_valid=n_valid, params=params,
                     features=features, metrics=metrics, artifact=artifact, note=note)
    session.add(record)
    await session.flush()
    return record


async def activate(session: AsyncSession, record: MlModel, now: datetime) -> None:
    """Делает модель активной; прежняя активная того же вида — retired."""
    await session.execute(update(MlModel).where(MlModel.kind == record.kind, MlModel.status == ACTIVE,
                                                MlModel.id != record.id).values(status=RETIRED))
    record.status, record.activated_at = ACTIVE, now
    await session.flush()


async def active_model(session: AsyncSession, kind: str) -> Optional[MlModel]:
    return (await session.execute(
        select(MlModel).where(MlModel.kind == kind, MlModel.status == ACTIVE).order_by(MlModel.id.desc()).limit(1)
    )).scalars().first()


async def get_model(session: AsyncSession, kind: str, version: str) -> Optional[MlModel]:
    return (await session.execute(
        select(MlModel).where(MlModel.kind == kind, MlModel.version == version))).scalars().first()


async def last_trained_at(session: AsyncSession, kind: str) -> Optional[datetime]:
    return (await session.execute(select(func.max(MlModel.trained_at)).where(MlModel.kind == kind))).scalar_one()


async def list_models(session: AsyncSession, kind: Optional[str] = None, limit: int = 50) -> list[MlModel]:
    """Версии без самих моделей (artifact не читается из базы)."""
    query = (select(MlModel).options(defer(MlModel.artifact))
             .order_by(MlModel.trained_at.desc(), MlModel.id.desc()).limit(limit))
    if kind:
        query = query.where(MlModel.kind == kind)
    return list((await session.execute(query)).scalars().all())
