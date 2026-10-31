# One image for the API (which also serves the web app) and the worker.
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr postgresql-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=web /web/dist /srv/web/dist
ENV WEB_DIST=/srv/web/dist DATA_DIR=/data PYTHONUNBUFFERED=1
RUN useradd --system --uid 10001 aeternix && mkdir -p /data && chown aeternix /data
USER aeternix
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
