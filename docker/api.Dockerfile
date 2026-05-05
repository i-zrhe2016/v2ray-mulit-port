FROM v2fly/v2fly-core:latest AS v2ray-bin

FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    API_PORT=2016

RUN apk add --no-cache ca-certificates docker-cli

WORKDIR /app

COPY --from=v2ray-bin /usr/bin/v2ray /usr/local/bin/v2ray
COPY api /app/api

EXPOSE 2016

CMD ["python", "-m", "api.server"]
