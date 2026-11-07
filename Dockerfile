FROM python:3.12-slim

WORKDIR /app

# postgresql-client provides pg_dump / psql for the backup feature
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY CHANGELOG.md .

RUN mkdir -p /data/backups /data/thumbs /data/imports /data/photos

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
