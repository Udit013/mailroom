FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e ".[dev]"

RUN useradd --create-home mailroom \
    && mkdir -p /data/documents \
    && chown -R mailroom /app /data/documents
USER mailroom
