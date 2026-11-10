FROM python:3.12-slim

WORKDIR /app
COPY . .
# The constraints pin indirect dependencies too, so a rebuild is the build that was tested.
RUN pip install --no-cache-dir -c constraints.txt -e ".[dev]"

RUN useradd --create-home mailroom \
    && mkdir -p /data/documents \
    && chown -R mailroom /app /data/documents
USER mailroom
