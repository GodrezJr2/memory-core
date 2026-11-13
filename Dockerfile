# /live page: React + Tailwind + Framer Motion, built to static files
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /srv
# OCR for saved references (English is included; Indonesian added)
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-ind \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY --from=web /web/dist ./app/live
USER 65534
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8420", "--proxy-headers", "--no-server-header"]
