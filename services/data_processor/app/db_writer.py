# services/data_processor/app/db_writer.py
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, Session

from app.schemas import ScrapedAdSchema
from app.models import AutoAd, CarMake, CarModel, AutoAdHistory

logger = logging.getLogger(__name__)


async def get_or_create_make(session: AsyncSession, make_name: str) -> Optional[CarMake]:
    if not make_name:
        return None
    slug = make_name.lower().replace(" ", "-")
    result = await session.execute(select(CarMake).where(CarMake.slug == slug))
    make = result.scalars().first()
    if not make:
        make = CarMake(name=make_name, slug=slug)
        session.add(make)
        await session.flush()
        logger.info(f"Создана новая марка: {make_name}")
    return make


async def get_or_create_model(session: AsyncSession, make: CarMake, model_name: str) -> Optional[CarModel]:
    if not model_name or not make:
        return None
    slug = f"{make.slug}-{model_name.lower().replace(' ', '-')}"
    result = await session.execute(select(CarModel).where(CarModel.slug == slug))
    model = result.scalars().first()
    if not model:
        model = CarModel(name=model_name, slug=slug, make_id=make.id)
        session.add(model)
        await session.flush()
        logger.info(f"Создана новая модель: {model_name} для марки {make.name}")
    return model


async def process_ad_data(session: Session, ad_data: ScrapedAdSchema):
    result = await session.execute(select(AutoAd).where(AutoAd.id_ad == ad_data.source_ad_id))
    existing_ad: Optional[AutoAd] = result.scalars().first()

    if existing_ad:
        logger.info(f"Обновление данных для объявления ID: {ad_data.source_ad_id}")

        update_data = ad_data.model_dump(by_alias=True, exclude_unset=True,
                                         exclude={'source_ad_id', 'source_name', 'country_code', 'scraped_at',
                                                  'description', 'image_urls', 'url_ad'})

        # Убираем информацию о часовом поясе из createdAt перед обновлением
        if 'createdAt' in update_data and update_data['createdAt'] and getattr(update_data['createdAt'], 'tzinfo',
                                                                               None):
            logger.info(f"Преобразование aware datetime в naive для {ad_data.source_ad_id}")
            update_data['createdAt'] = update_data['createdAt']

        price_changed = 'price' in update_data and existing_ad.price != update_data.get('price')

        for key, value in update_data.items():
            setattr(existing_ad, key, value)

        if price_changed:
            logger.info(
                f"Цена изменилась для {ad_data.source_ad_id}: {existing_ad.price} -> {update_data.get('price')}")
            history_entry = AutoAdHistory(
                auto_ad_id=existing_ad.id_ad,
                price=update_data.get('price'),
                currencyCode=update_data.get('currencyCode'),
                status="price_changed"
            )
            session.add(history_entry)
        session.add(existing_ad)

    else:
        logger.info(f"Создание нового объявления ID: {ad_data.source_ad_id}")
        make = await get_or_create_make(session, ad_data.make_str)
        model = await get_or_create_model(session, make, ad_data.model_str)

        new_ad_dict = ad_data.model_dump(
            by_alias=True,
            exclude_unset=True,
            exclude={"source_ad_id", "country_code", "scraped_at", "description", "image_urls"}
        )

        # Убираем информацию о часовом поясе из createdAt перед созданием
        if 'createdAt' in new_ad_dict and new_ad_dict['createdAt'] and getattr(new_ad_dict['createdAt'], 'tzinfo',
                                                                               None):
            new_ad_dict['createdAt'] = new_ad_dict['createdAt']
        elif 'createdAt' not in new_ad_dict or not new_ad_dict.get('createdAt'):
            new_ad_dict['createdAt'] = datetime.utcnow()

        new_ad = AutoAd(id_ad=ad_data.source_ad_id, **new_ad_dict)
        new_ad.car_model_id = model.id if model else None

        session.add(new_ad)

        history_entry = AutoAdHistory(
            auto_ad_id=new_ad.id_ad,
            price=new_ad.price,
            currencyCode=new_ad.currencyCode,
            status="active"
        )
        session.add(history_entry)
