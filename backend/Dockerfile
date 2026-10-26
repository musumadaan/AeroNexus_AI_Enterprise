FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AERONEXUS_STATE_DB=/app/state/state.db
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# synthetic corpus + public datasets (checksum-verified) + models; set TRAIN=0 to skip training at build time
ARG TRAIN=1
RUN python scripts/generate_data.py --train 600 \
 && python scripts/fetch_public_data.py --only cmapss,ntsb \
 && if [ "$TRAIN" = "1" ] && [ ! -f models/pdm_FD004.joblib ]; then python scripts/train_public_models.py; fi \
 && AERONEXUS_STATE_DB=:memory: python -m aeronexus.evals.runner
RUN useradd -m app && mkdir -p /app/state && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "aeronexus.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
