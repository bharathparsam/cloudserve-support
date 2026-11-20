FROM node:22.21.1-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
FROM python:3.12.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/app/storage/cache/huggingface ANONYMIZED_TELEMETRY=False
WORKDIR /app
ARG TARGETARCH
COPY requirements-linux-*.txt ./
RUN pip install --no-cache-dir --no-deps --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu && pip install --no-cache-dir -r requirements-linux-${TARGETARCH}.txt
COPY requirements.txt .
RUN groupadd -g 10001 app && useradd -m -u 10001 -g app app
COPY --chown=app:app . .
COPY --from=frontend --chown=app:app /build/dist ./frontend/dist
RUN mkdir -p storage && chown app:app storage
USER app
EXPOSE 8000
CMD ["python","-m","uvicorn","src.api:app","--host","0.0.0.0","--port","8000","--workers","1"]
