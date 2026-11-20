# Сборка из корня репозитория: docker build -f server/Dockerfile -t lawtracker .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY server/requirements.txt server/requirements-prod.txt /srv/server/
RUN pip install -r /srv/server/requirements-prod.txt

COPY server /srv/server
COPY index.html /srv/index.html
COPY logo /srv/logo
COPY deploy/certs /srv/certs

RUN useradd --create-home --uid 10001 app && chown -R app /srv
USER app
WORKDIR /srv/server
ENV SITE_FILE=/srv/index.html APP_ENV=production AUTO_CREATE_TABLES=false

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
