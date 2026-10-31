#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

CONFIG=/data/options.json
opt() { jq -r --arg k "$1" '.[$k] // empty' "$CONFIG"; }

PORT=$(opt port)
BAUD=$(opt baud)
ADDRESSES=$(opt addresses)
DIALECT=$(opt dialect)
INTERVAL=$(opt interval)
MQTT_HOST=$(opt mqtt_host)
MQTT_PORT=$(opt mqtt_port)
MQTT_USER=$(opt mqtt_user)
MQTT_PASSWORD=$(opt mqtt_password)
BASE_TOPIC=$(opt base_topic)
HA_PREFIX=$(opt ha_prefix)
HIGH_RES=$(opt high_res_soc)
LOG_LEVEL=$(opt log_level)

# Fall back to the Supervisor-provided MQTT service when the user left host/user empty.
if command -v bashio >/dev/null 2>&1 && bashio::services.available mqtt; then
  [ -z "$MQTT_HOST" ] || [ "$MQTT_HOST" = "core-mosquitto" ] && MQTT_HOST=$(bashio::services mqtt "host")
  [ -z "$MQTT_PORT" ] && MQTT_PORT=$(bashio::services mqtt "port")
  [ -z "$MQTT_USER" ] && MQTT_USER=$(bashio::services mqtt "username")
  [ -z "$MQTT_PASSWORD" ] && MQTT_PASSWORD=$(bashio::services mqtt "password")
fi

ARGS=(poll --port "$PORT" --baud "${BAUD:-9600}" --addresses "${ADDRESSES:-1}" --dialect "${DIALECT:-auto}"
      --interval "${INTERVAL:-5}" --mqtt "${MQTT_HOST}:${MQTT_PORT:-1883}"
      --base-topic "${BASE_TOPIC:-jbd_up}" --ha-prefix "${HA_PREFIX:-homeassistant}")
[ -n "$MQTT_USER" ] && ARGS+=(--user "$MQTT_USER")
[ -n "$MQTT_PASSWORD" ] && ARGS+=(--pass "$MQTT_PASSWORD")
[ "$HIGH_RES" = "false" ] && ARGS+=(--no-high-res-soc)
case "$LOG_LEVEL" in
  debug) ARGS+=(-vv) ;;
  info) ARGS+=(-v) ;;
esac

echo "[jbd-up] starting: jbd-up ${ARGS[*]//$MQTT_PASSWORD/***}"
exec jbd-up "${ARGS[@]}"
