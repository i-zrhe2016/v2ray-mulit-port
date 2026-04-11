FROM v2fly/v2fly-core:latest

ENV V2RAY_API_PORT=10085 \
    V2RAY_LOG_LEVEL=warning

COPY docker/entrypoint.sh /entrypoint.sh
COPY v2ray/config.template.json /etc/v2ray/config.template.json

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
