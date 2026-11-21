# Map web app. Build context is the web/ folder, with ahs.db copied into it first.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static
COPY ahs.db ./ahs.db
# Switch the database out of WAL mode so it can be opened read-only in a read-only folder.
RUN python -c "import sqlite3; c=sqlite3.connect('ahs.db'); c.execute('PRAGMA journal_mode=DELETE'); c.close()"

RUN useradd --system app
USER app

ENV AHS_DB=/app/ahs.db
# Cloud Run sets $PORT (8080 by default).
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
