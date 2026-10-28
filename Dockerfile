FROM rclone/rclone:1.75.2 AS rclone
FROM python:3.13-slim
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
WORKDIR /app
COPY remote-rag/requirements.txt /app/remote-rag/requirements.txt
RUN pip install --no-cache-dir -r /app/remote-rag/requirements.txt
COPY uni-rag/app.py uni-rag/embedding_api.py /app/uni-rag/
COPY skills/uni-kontext/scripts/uni_context.py /app/skills/uni-kontext/scripts/uni_context.py
COPY remote-rag/auth.py remote-rag/core.py remote-rag/server.py /app/remote-rag/
ENV PYTHONUNBUFFERED=1 UNI_RAG_STATE=/data FASTMCP_LOG_LEVEL=WARNING
CMD ["python", "/app/remote-rag/server.py"]
