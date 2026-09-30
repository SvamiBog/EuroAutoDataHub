# services/api_service/Dockerfile (контекст сборки — корень репозитория)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Устанавливаем рабочую директорию
WORKDIR /app

# Зависимости и общий пакет модели данных
COPY ./libs/eadh_common /opt/eadh_common
COPY ./services/api_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /opt/eadh_common

# Копируем остальной код сервиса в контейнер
COPY ./services/api_service/ .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
