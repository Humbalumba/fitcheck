# FitCheck backend (FastAPI + CPU PyTorch). Model weights (~1.7 GB) are NOT baked in: mount backend/models at
# /app/models (read-only). To bake them into the image instead (e.g. for a PaaS without volumes): remove "models"
# from backend/.dockerignore and add `COPY models ./models` below.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
    FITCHECK_DATA_DIR=/data FITCHECK_ENV=production
WORKDIR /app
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.0
COPY requirements-docker.txt .
RUN pip install -r requirements-docker.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini docker-entrypoint.sh ./
RUN useradd -u 10001 -m fitcheck && mkdir -p /data /app/models && chown -R fitcheck /data && chmod +x docker-entrypoint.sh
USER fitcheck
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health',timeout=4)"
ENTRYPOINT ["./docker-entrypoint.sh"]
