FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY news_bot /app/news_bot
COPY README.md /app/README.md

RUN mkdir -p /app/data

CMD ["python", "-m", "news_bot", "run"]
