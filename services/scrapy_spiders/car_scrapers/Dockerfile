# Используем официальный образ Python
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Устанавливаем рабочую директорию
WORKDIR /usr/src/app

# Устанавливаем зависимости паука (БД и код других сервисов пауку не нужны)
COPY ./services/scrapy_spiders/car_scrapers/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код самого Scrapy-проекта
COPY ./services/scrapy_spiders/car_scrapers/ .

# Команда по умолчанию
CMD ["scrapy", "list"]
