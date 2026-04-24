FROM python:3.11-slim

WORKDIR /app

COPY webapp/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir --upgrade-strategy=only-if-needed -r /tmp/requirements.txt

COPY webapp /app

ENV PYTHONUNBUFFERED=1 \
    STATIC_DIR=/app/static \
    TENANT_CONFIG_FILE=/app/secrets/tenants.json \
    PORT=8000

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
