FROM python:3.12-slim

# Don't buffer stdout/stderr so logs show up in `docker logs` immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pausarr ./pausarr

# State is persisted here; mount a volume to keep it across restarts.
ENV STATE_FILE=/data/state.json
VOLUME ["/data"]

EXPOSE 8080

# Container-level healthcheck hitting the liveness endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz').status==200 else 1)" || exit 1

CMD ["uvicorn", "pausarr.app:app", "--host", "0.0.0.0", "--port", "8080"]
