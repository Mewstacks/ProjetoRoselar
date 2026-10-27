FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# manage.py vive em /app/config; BASE_DIR do settings resolve para /app.
WORKDIR /app/config
RUN python manage.py collectstatic --no-input

EXPOSE 8000

# Uma Machine de 256MB: 1 worker com threads. Reciclar o worker limita o
# crescimento de memoria do reportlab/Pillow ao gerar PDF com fotos.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "4", \
     "--timeout", "120", \
     "--max-requests", "1000", "--max-requests-jitter", "100"]
