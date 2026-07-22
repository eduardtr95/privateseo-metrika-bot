FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY metrika_bot ./metrika_bot
RUN pip install --no-cache-dir .

RUN mkdir -p /data && chown app:app /data
USER app

ENV DATABASE_PATH=/data/metrika-bot.sqlite3 \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080

EXPOSE 8080
CMD ["metrika-bot"]
