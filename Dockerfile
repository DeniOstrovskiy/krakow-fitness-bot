FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    USE_PLAYWRIGHT=0 \
    TIMEZONE=Europe/Warsaw

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .
CMD ["python", "run.py"]
