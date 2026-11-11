#!/bin/sh
set -eu
ip -4 route get 9.9.9.9 from 198.19.0.1 | grep -q 'dev oflux0 table 201' || {
  echo 'TUN routing is not ready.'
  exit 1
}
result=$(curl -4 -fsS --interface 198.19.0.1 --connect-timeout 15 --max-time 35 https://api.ipify.org) || {
  echo 'TUN HTTPS check failed.'
  exit 1
}
[ "$result" = '37.252.5.64' ] || { echo 'Unexpected TUN exit.'; exit 1; }
