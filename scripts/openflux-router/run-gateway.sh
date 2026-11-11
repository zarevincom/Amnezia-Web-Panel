#!/bin/sh
set -eu
umask 077
/usr/bin/sing-box run -c /etc/openflux/gateway.json > /tmp/openflux-gateway.log 2>&1 &
child=$!
cleanup() {
  trap - EXIT INT TERM HUP
  /etc/openflux/route-down.sh
  kill "$child" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM HUP
ready=0
for attempt in $(seq 1 15); do
  kill -0 "$child" 2>/dev/null || exit 1
  if ip link show oflux0 >/dev/null 2>&1 &&
     curl -4 -fsS --socks5-hostname 127.0.0.1:1080 --connect-timeout 15 --max-time 35 https://api.ipify.org > /tmp/openflux-exit-ip 2>/dev/null &&
     [ "$(cat /tmp/openflux-exit-ip)" = '37.252.5.64' ]; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" = 1 ] || { logger -t openflux 'SOCKS health check failed; VK TURN routing retained'; exit 1; }
ip -4 route replace default dev oflux0 table 201 metric 10
ip -4 route replace unreachable default table 201 metric 32760
ip -4 rule del priority 43 from 198.19.0.1/32 lookup 201 2>/dev/null || true
ip -4 rule add priority 43 from 198.19.0.1/32 lookup 201
if [ -f /etc/openflux/route-enabled ]; then
  consecutive=0
  for attempt in $(seq 1 9); do
    if /etc/openflux/check-tun.sh >/tmp/openflux-startup-tun.log 2>&1; then
      consecutive=$((consecutive + 1))
      if [ "$consecutive" -ge 3 ]; then break; fi
    else
      consecutive=0
    fi
    sleep 2
  done
  [ "$consecutive" -ge 3 ] || {
    logger -t openflux 'TUN startup check failed; VK TURN routing retained'
    exit 1
  }
  /etc/openflux/route-up.sh
fi
logger -t openflux 'TUN gateway ready; exit 37.252.5.64'
wait "$child"
