FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so layer caching survives source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

COPY . .

EXPOSE 8000

# The dataset lives in a volume, so migrate on start and hand over to gunicorn.
CMD ["sh", "-c", "python manage.py migrate --noinput && \
     gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60"]
