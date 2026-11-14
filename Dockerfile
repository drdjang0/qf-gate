FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd --create-home app && mkdir -p /app/var /app/staticfiles && chown -R app /app
USER app

# collectstatic needs no DB; a throwaway key is fine at build time only
RUN DJANGO_DEBUG=false DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "qfgate.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
