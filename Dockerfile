FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY maps ./maps
COPY deploy/generate-cert.sh /generate-cert.sh
COPY deploy/app-entrypoint.sh /app-entrypoint.sh
RUN chmod +x /generate-cert.sh /app-entrypoint.sh

ENV HERITAGE_DATA_DIR=/data
EXPOSE 8000
ENTRYPOINT ["/app-entrypoint.sh"]
