# services/data_processor/Dockerfile (контекст сборки — корень репозитория)
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Зависимости и общий пакет модели данных
COPY ./libs/eadh_common /opt/eadh_common
COPY ./services/data_processor/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt /opt/eadh_common

# Код ingestor
COPY ./services/data_processor/app /app/app

CMD ["python", "-m", "app.ingestor"]
