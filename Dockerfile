FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 lightcard \
    && useradd --uid 10001 --gid lightcard --no-create-home lightcard \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data \
    && chmod 700 /app/data

COPY app.py ./
COPY lightcard_hermes.py ./
COPY lightcard_sub2api.py sub2api_adapter.py ./
COPY static/ ./static/
COPY scripts/backup.py ./scripts/backup.py
COPY scripts/redeem_worker.py ./scripts/redeem_worker.py
COPY scripts/gunicorn.conf.py ./scripts/gunicorn.conf.py
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
CMD ["gunicorn", "--config", "scripts/gunicorn.conf.py", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "30", "--graceful-timeout", "30", "--worker-tmp-dir", "/tmp", "--max-requests", "1000", "--max-requests-jitter", "50", "app:create_app()"]
