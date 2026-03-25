# ─── Stage 1: Bootstrap — generate synthetic models ──────────────────────────
# This stage only runs on first deploy or when --force is passed.
# In CI, pre-trained model artifacts are cached between builds.
FROM python:3.12-slim AS model-builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ml/ ml/

# Generate bootstrap models
ARG BOOTSTRAP_SAMPLES=500
ARG BOOTSTRAP_REGIONS="us-east-1,us-west-2,eu-west-1"
RUN python -m ml.pipeline.bootstrap \
    --model-dir /opt/nimbusnet/models \
    --data-dir  /opt/nimbusnet/data \
    --samples   ${BOOTSTRAP_SAMPLES} \
    --regions   "${BOOTSTRAP_REGIONS}"

# ─── Stage 2: Production serving image ────────────────────────────────────────
FROM python:3.12-slim AS serving

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*

RUN useradd -r -s /bin/false nimbusnet

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ml/ ml/

# Copy bootstrap models from builder stage
COPY --from=model-builder /opt/nimbusnet/models /opt/nimbusnet/models
COPY --from=model-builder /opt/nimbusnet/data    /opt/nimbusnet/data

COPY configs/ /etc/nimbusnet/

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8001/models/health || exit 1

USER nimbusnet

EXPOSE 8001

ENV MODEL_DIR=/opt/nimbusnet/models
ENV DATA_DIR=/opt/nimbusnet/data
ENV PORT=8001

CMD ["python", "-m", "ml.serving.main"]
