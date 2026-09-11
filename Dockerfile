FROM node:24-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    NUMBA_CACHE_DIR=/app/backend/data/cache/numba U2NET_HOME=/models
COPY backend/ ./backend/
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --constraint backend/constraints.lock ./backend
COPY --from=frontend /app/frontend/dist ./frontend/dist
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/backend/data/results /app/backend/data/tmp /app/backend/data/cache/numba /models \
    && chown -R app:app /app/backend/data /models
USER app
# Exercise real rembg/PyMatting imports as the unprivileged production user.
RUN PYTHONPATH=/app/backend python -c "from app.services.image_runtime import warmup_image_runtime; warmup_image_runtime()"
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
