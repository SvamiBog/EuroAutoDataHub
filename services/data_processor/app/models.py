# services/data_processor/app/models.py
from datetime import datetime
from sqlalchemy import Column, DateTime
from typing import List, Optional
from sqlmodel import SQLModel, Field, Relationship


class CarMake(SQLModel, table=True):
    __tablename__ = "car_make"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    slug: str = Field(index=True, unique=True)

    models: List["CarModel"] = Relationship(back_populates="make")


class CarModel(SQLModel, table=True):
    __tablename__ = "car_model"

    id: Optional[int] = Field(default=None, primary_key=True)
    make_id: int = Field(foreign_key="car_make.id", index=True)
    name: str = Field(index=True)
    slug: str = Field(index=True, unique=True)

    make: Optional[CarMake] = Relationship(back_populates="models")
    ads: List["AutoAd"] = Relationship(back_populates="car_model")


class AutoAd(SQLModel, table=True):
    __tablename__ = "auto_ad"

    id_ad: str = Field(default=None, primary_key=True, index=True)
    make_name: Optional[str] = Field(default=None, alias="make")
    model_name: Optional[str] = Field(default=None, alias="model")
    version: Optional[str] = Field(default=None)
    generation: Optional[str] = Field(default=None, index=True)
    year: Optional[int] = Field(default=None, index=True)
    title: Optional[str] = Field(default=None)
    url_ad: Optional[str] = Field(default=None, index=True, unique=True)
    city: Optional[str] = Field(default=None, index=True)
    region: Optional[str] = Field(default=None)
    price: Optional[int] = Field(default=None, index=True)
    currencyCode: Optional[str] = Field(default=None, max_length=3)
    fuel_type: Optional[str] = Field(default=None)
    gearbox: Optional[str] = Field(default=None)
    mileage: Optional[int] = Field(default=None, index=True)
    engine_capacity: Optional[int] = Field(default=None)
    color: Optional[str] = Field(default=None)
    transmission: Optional[str] = Field(default=None)
    engine_power: Optional[int] = Field(default=None)
    sellerLink: Optional[str] = Field(default=None)
    createdAt: datetime = Field(sa_column=Column(DateTime(timezone=True)))
    sold_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    source_name: Optional[str] = Field(default=None, index=True)

    car_model_id: Optional[int] = Field(default=None, foreign_key="car_model.id", index=True)
    car_model: Optional["CarModel"] = Relationship(back_populates="ads")

    history: List["AutoAdHistory"] = Relationship(
        back_populates="auto_ad",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )


class AutoAdHistory(SQLModel, table=True):
    __tablename__ = "auto_ad_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    auto_ad_id: str = Field(foreign_key="auto_ad.id_ad", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    price: Optional[int] = Field(default=None)
    currencyCode: Optional[str] = Field(default=None, max_length=3)
    status: Optional[str] = Field(default=None)

    auto_ad: Optional[AutoAd] = Relationship(back_populates="history")