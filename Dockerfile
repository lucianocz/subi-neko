# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:26-alpine AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime ───────────────────────────────────────────────────
FROM python:3.12-slim

ENV PUID=1000
ENV PGID=1000
ENV UMASK=002

WORKDIR /app

# System tools required at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ ./

# Copy built frontend into static/ so FastAPI can serve it
COPY --from=frontend-builder /app/frontend/dist ./static

# Create default directories (overridable via env / volume mounts)
RUN mkdir -p /app/config /app/media/import /app/media/output

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
