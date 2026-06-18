# Базовый образ с Python
FROM python:3.11-slim

# Системные зависимости: ffmpeg обязателен для нарезки видео и вшивания субтитров
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала копируем только requirements - так Docker кеширует слой с зависимостями
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем сам код бота
COPY . .

# Render должен запускать воркер (не веб-сервис), но Dockerfile подходит для обоих
CMD ["python", "bot.py"]
