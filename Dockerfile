FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# data/ holds the SQLite DB, media, and the Gmail token — mount it as a volume
RUN mkdir -p data

# Render (and similar hosts) inject PORT; default to 8000 locally
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
