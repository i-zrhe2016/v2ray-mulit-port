FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_PORT=2016

WORKDIR /app

COPY api/server.py /app/server.py

EXPOSE 2016

CMD ["python", "/app/server.py"]
