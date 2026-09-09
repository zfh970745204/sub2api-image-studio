FROM node:24-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/ ./backend/
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --constraint backend/constraints.lock ./backend
COPY --from=frontend /app/frontend/dist ./frontend/dist
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/backend/data/results /app/backend/data/tmp /models \
    && chown -R app:app /app/backend/data /models
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
