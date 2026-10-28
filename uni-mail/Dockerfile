FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils ocrmypdf tesseract-ocr-deu tesseract-ocr-eng ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY remote-rag/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY remote-rag/auth.py /app/remote-rag/auth.py
COPY uni-mail/core.py uni-mail/server.py uni-mail/https_mail.py uni-mail/pilot_state.py /app/uni-mail/
ENV PYTHONUNBUFFERED=1 UNI_MAIL_STATE=/data FASTMCP_LOG_LEVEL=WARNING
CMD ["python", "/app/uni-mail/server.py"]
