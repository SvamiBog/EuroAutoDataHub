# services/data_processor/app/normalization.py
"""Сопоставление значений марки и модели площадки со справочниками vehicle_make / vehicle_model.

Неизвестное значение автоматически заводится в справочник (slug = slugify(значение)) и
получает алиас. Алиас позже можно перенаправить вручную на другую каноничную запись.
"""
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from eadh_common.models import MakeAlias, ModelAlias, VehicleMake, VehicleModel
from eadh_common.normalize import slugify


class Normalizer:
    """Кэширует алиасы между батчами.

    Записи, созданные в текущей транзакции, лежат в `_pending` и попадают в кэш только после
    commit(): иначе после отката транзакции кэш ссылался бы на несуществующие id.
    """

    def __init__(self):
        self._makes: dict[tuple[str, str], int] = {}
        self._models: dict[tuple[str, int, str], int] = {}
        self._pending_makes: dict[tuple[str, str], int] = {}
        self._pending_models: dict[tuple[str, int, str], int] = {}

    def commit(self) -> None:
        self._makes.update(self._pending_makes)
        self._models.update(self._pending_models)
        self.rollback()

    def rollback(self) -> None:
        self._pending_makes.clear()
        self._pending_models.clear()

    async def resolve(self, session: AsyncSession, source: str, make_raw: Optional[str],
                      model_raw: Optional[str]) -> tuple[Optional[int], Optional[int]]:
        """Возвращает (make_id, model_id) для значений площадки."""
        make_id = await self._resolve_make(session, source, make_raw)
        if make_id is None:
            return None, None
        return make_id, await self._resolve_model(session, source, make_id, model_raw)

    async def _resolve_make(self, session, source, make_raw) -> Optional[int]:
        raw = (make_raw or "").strip().lower()
        slug = slugify(raw)
        if not slug:
            return None
        key = (source, raw)
        cached = self._pending_makes.get(key) or self._makes.get(key)
        if cached:
            return cached

        alias = (await session.execute(
            select(MakeAlias.make_id).where(MakeAlias.source == source, MakeAlias.raw_value == raw)
        )).scalar_one_or_none()
        if alias is not None:
            self._makes[key] = alias
            return alias

        make = (await session.execute(select(VehicleMake).where(VehicleMake.slug == slug))).scalar_one_or_none()
        if make is None:
            make = VehicleMake(slug=slug, name=raw)
            session.add(make)
            await session.flush()
        session.add(MakeAlias(source=source, raw_value=raw, make_id=make.id))
        self._pending_makes[key] = make.id
        return make.id

    async def _resolve_model(self, session, source, make_id, model_raw) -> Optional[int]:
        raw = (model_raw or "").strip().lower()
        slug = slugify(raw)
        if not slug:
            return None
        key = (source, make_id, raw)
        cached = self._pending_models.get(key) or self._models.get(key)
        if cached:
            return cached

        alias = (await session.execute(
            select(ModelAlias.model_id).where(
                ModelAlias.source == source, ModelAlias.make_id == make_id, ModelAlias.raw_value == raw)
        )).scalar_one_or_none()
        if alias is not None:
            self._models[key] = alias
            return alias

        model = (await session.execute(
            select(VehicleModel).where(VehicleModel.make_id == make_id, VehicleModel.slug == slug)
        )).scalar_one_or_none()
        if model is None:
            model = VehicleModel(make_id=make_id, slug=slug, name=raw)
            session.add(model)
            await session.flush()
        session.add(ModelAlias(source=source, make_id=make_id, raw_value=raw, model_id=model.id))
        self._pending_models[key] = model.id
        return model.id
