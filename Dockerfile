FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 ibkr-note4
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /app/output /app/state && chown -R ibkr-note4:ibkr-note4 /app
USER ibkr-note4

VOLUME ["/app/output", "/app/state"]
CMD ["ibkr-note4", "run"]
