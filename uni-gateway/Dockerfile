FROM python:3.13-slim
WORKDIR /app
COPY uni-gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY remote-rag/auth.py /app/auth.py
COPY uni-gateway/server.py uni-gateway/calendar_sync.py /app/
ENV PYTHONUNBUFFERED=1 UNI_GATEWAY_STATE=/data FASTMCP_LOG_LEVEL=WARNING
CMD ["python", "/app/server.py"]
