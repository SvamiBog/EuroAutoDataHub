# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /usr/src/app

# Копируем файл с зависимостями (из контекста сборки)
COPY requirements.txt .

# Копируем файл с марками в контейнер
COPY make_car.json /usr/src/make_car.json

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект Scrapy (из контекста сборки)
# Это скопирует и scrapy.cfg, и вложенную папку car_scrapers
COPY . .

# Команда по умолчанию (необязательно, но полезно)
CMD ["scrapy", "crawl"]
