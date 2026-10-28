FROM rclone/rclone:1.75.2 AS rclone
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils ca-certificates ocrmypdf tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
WORKDIR /app
COPY sync.py /app/sync.py
COPY rag_index.py /app/rag_index.py
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "/app/sync.py"]
