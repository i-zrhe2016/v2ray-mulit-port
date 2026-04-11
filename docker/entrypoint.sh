#!/bin/sh
set -eu

TEMPLATE_FILE="/etc/v2ray/config.template.json"
CONFIG_FILE="/etc/v2ray/config.json"

escape_sed() {
  printf '%s' "$1" | sed 's/[\/&|]/\\&/g'
}

V2RAY_API_PORT="${V2RAY_API_PORT:-10085}"
V2RAY_LOG_LEVEL="${V2RAY_LOG_LEVEL:-warning}"

API_PORT_ESCAPED="$(escape_sed "$V2RAY_API_PORT")"
LOG_LEVEL_ESCAPED="$(escape_sed "$V2RAY_LOG_LEVEL")"

sed \
  -e "s|__V2RAY_API_PORT__|$API_PORT_ESCAPED|g" \
  -e "s|__V2RAY_LOG_LEVEL__|$LOG_LEVEL_ESCAPED|g" \
  "$TEMPLATE_FILE" > "$CONFIG_FILE"

exec v2ray run -config "$CONFIG_FILE"
