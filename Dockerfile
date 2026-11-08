FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app/backend

COPY backend/ ./
COPY frontend/ /app/frontend/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host \"$HOST\" --port \"$PORT\" --workers 1"]
