FROM v2fly/v2fly-core:latest

ENV V2RAY_PORT=10086 \
    V2RAY_UUID=640b6169-cd66-4d42-bb8f-25b62930e925 \
    V2RAY_ALTER_ID=0 \
    V2RAY_WS_PATH=/ray \
    V2RAY_LOG_LEVEL=warning

COPY docker/entrypoint.sh /entrypoint.sh
COPY v2ray/config.template.json /etc/v2ray/config.template.json

ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
