FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Samara

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Всё состояние (БД, бэкапы, решения, загрузки, логи) — в /app/data, это volume.
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "-m", "app"]
