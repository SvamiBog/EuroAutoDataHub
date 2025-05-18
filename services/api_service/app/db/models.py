# services/api_service/app/db/models.py
from datetime import datetime
from typing import List, Optional  # Убедитесь, что List и Optional импортированы из typing
from sqlmodel import SQLModel, Field, Relationship  # SQLModel импортируется здесь


class CarMake(SQLModel, table=True):
    # __tablename__ можно не указывать, SQLModel сгенерирует его из имени класса (car_make)
    # но явное указание - хорошая практика
    __tablename__ = "car_make"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)  # Рекомендую добавить index=True для часто фильтруемых полей
    slug: str = Field(index=True, unique=True)

    models: List["CarModel"] = Relationship(back_populates="make")


class CarModel(SQLModel, table=True):
    __tablename__ = "car_model"

    id: Optional[int] = Field(default=None, primary_key=True)
    make_id: int = Field(foreign_key="car_make.id", index=True)  # Убедитесь, что foreign_key правильный
    name: str = Field(index=True)
    slug: str = Field(index=True, unique=True)

    make: Optional[CarMake] = Relationship(back_populates="models")
    ads: List["AutoAd"] = Relationship(back_populates="car_model")


class AutoAd(SQLModel, table=True):
    __tablename__ = "auto_ad"

    # id_ad: str = Field(primary_key=True, index=True) # Имя таблицы во внешнем ключе AutoAdHistory должно совпадать
    id_ad: str = Field(default=None, primary_key=True,
                       index=True)  # Если это не генерируется само, default=None обязательно

    createdAt: datetime  # Рекомендую default_factory=datetime.utcnow

    make_name: Optional[str] = Field(default=None, alias="make")
    model_name: Optional[str] = Field(default=None, alias="model")

    version: Optional[str] = Field(default=None)
    year: Optional[int] = Field(default=None, index=True)
    title: Optional[str] = Field(default=None)
    url_ad: Optional[str] = Field(default=None, index=True, unique=True)  # Если URL должен быть уникальным
    city: Optional[str] = Field(default=None, index=True)
    region: Optional[str] = Field(default=None)
    price: Optional[int] = Field(default=None, index=True)
    currencyCode: Optional[str] = Field(default=None, max_length=3)  # max_length для строк полезно
    fuel_type: Optional[str] = Field(default=None)
    gearbox: Optional[str] = Field(default=None)
    mileage: Optional[int] = Field(default=None, index=True)
    engine_capacity: Optional[int] = Field(default=None)
    color: Optional[str] = Field(default=None)
    transmission: Optional[str] = Field(default=None)  # Повторяется с gearbox?
    engine_power: Optional[int] = Field(default=None)
    sellerLink: Optional[str] = Field(default=None)
    sold_at: Optional[datetime] = Field(default=None)

    car_model_id: Optional[int] = Field(default=None, foreign_key="car_model.id", index=True)
    car_model_rel: Optional["CarModel"] = Relationship(back_populates="ads",
                                                       sa_relationship_kwargs={'foreign_keys': "[AutoAd.car_model_id]"})

    history: List["AutoAdHistory"] = Relationship(
        back_populates="auto_ad",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class AutoAdHistory(SQLModel, table=True):
    __tablename__ = "auto_ad_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    auto_ad_id: str = Field(foreign_key="auto_ad.id_ad",
                            index=True)  # Убедитесь, что "auto_ad.id_ad" соответствует PK в AutoAd
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    price: Optional[int] = Field(default=None)
    currencyCode: Optional[str] = Field(default=None, max_length=3)
    status: Optional[str] = Field(default=None)  # Например "active", "sold", "expired"

    auto_ad: Optional[AutoAd] = Relationship(back_populates="history")