# services/data_processor/Dockerfile

# 1. Базовый образ
FROM python:3.11-slim

# 2. Установка рабочей директории
WORKDIR /app

# 3. Установка переменных окружения
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 4. Установка зависимостей
# Копируем сначала только файлы зависимостей для кэширования этого слоя
COPY ./services/data_processor/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 5. Копирование кода приложения
# ВАЖНО: Копируем модели из api_service в наше приложение
# Это временное решение. В идеале - общая библиотека.
COPY ./services/api_service/app/db/models.py /app/app/models.py
# Создаем пустой __init__.py, чтобы models.py был частью пакета app
RUN touch /app/app/__init__.py

# Копируем остальной код data_processor
COPY ./services/data_processor/app /app/app

# 6. Команда для запуска приложения
CMD ["python", "-m", "app.consumer"]