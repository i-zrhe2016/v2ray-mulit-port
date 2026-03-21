#!/bin/sh
set -eu

TEMPLATE_FILE="/etc/v2ray/config.template.json"
CONFIG_FILE="/etc/v2ray/config.json"

escape_sed() {
  printf '%s' "$1" | sed 's/[\/&|]/\\&/g'
}

V2RAY_PORT="${V2RAY_PORT:-10086}"
V2RAY_UUID="${V2RAY_UUID:?V2RAY_UUID is required}"
V2RAY_ALTER_ID="${V2RAY_ALTER_ID:-0}"
V2RAY_WS_PATH="${V2RAY_WS_PATH:-/ray}"
V2RAY_LOG_LEVEL="${V2RAY_LOG_LEVEL:-warning}"

PORT_ESCAPED="$(escape_sed "$V2RAY_PORT")"
UUID_ESCAPED="$(escape_sed "$V2RAY_UUID")"
ALTER_ID_ESCAPED="$(escape_sed "$V2RAY_ALTER_ID")"
WS_PATH_ESCAPED="$(escape_sed "$V2RAY_WS_PATH")"
LOG_LEVEL_ESCAPED="$(escape_sed "$V2RAY_LOG_LEVEL")"

sed \
  -e "s|__V2RAY_PORT__|$PORT_ESCAPED|g" \
  -e "s|__V2RAY_UUID__|$UUID_ESCAPED|g" \
  -e "s|__V2RAY_ALTER_ID__|$ALTER_ID_ESCAPED|g" \
  -e "s|__V2RAY_WS_PATH__|$WS_PATH_ESCAPED|g" \
  -e "s|__V2RAY_LOG_LEVEL__|$LOG_LEVEL_ESCAPED|g" \
  "$TEMPLATE_FILE" > "$CONFIG_FILE"

exec v2ray run -config "$CONFIG_FILE"
