FROM python:3.12-slim

LABEL org.opencontainers.image.title="Account Cleanup" \
      org.opencontainers.image.description="Self-hosted tracker for gradually deleting old online accounts" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/accounts.db \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .
COPY entrypoint.sh /entrypoint.sh
RUN mkdir -p /data

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=1m --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "app:app"]
