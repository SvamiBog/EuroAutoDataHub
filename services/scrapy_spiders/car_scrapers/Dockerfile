# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем рабочую директорию
WORKDIR /usr/src/app

# Копируем файлы зависимостей из их расположения в проекте
COPY ./services/scrapy_spiders/car_scrapers/requirements.txt ./scrapy_requirements.txt
COPY ./services/data_processor/requirements.txt ./data_processor_requirements.txt

# Устанавливаем ВСЕ необходимые зависимости
RUN pip install --no-cache-dir -r scrapy_requirements.txt
RUN pip install --no-cache-dir -r data_processor_requirements.txt

# Копируем код самого Scrapy-проекта
COPY ./services/scrapy_spiders/car_scrapers/ .

# --- КОНЕЦ ИЗМЕНЕНИЙ ---

# Настраиваем PYTHONPATH для доступа к data_processor (остается как есть)
# Этот путь будет создан через volumes в docker-compose
ENV PYTHONPATH=/usr/src/app:/usr/src/data_processor

# Команда по умолчанию
CMD ["scrapy", "list"]
