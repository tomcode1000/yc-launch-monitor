FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yml .

# State lives here. Mount a volume to keep dedupe history across redeploys;
# without one, a restart re-seeds from the directory (silently, no alert flood).
RUN mkdir -p /app/data
ENV DATABASE_PATH=/app/data/monitor.db
ENV PORT=8000

EXPOSE 8000
CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT}"]
