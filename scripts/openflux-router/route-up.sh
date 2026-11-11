#!/bin/sh
set -eu
ip link show oflux0 >/dev/null
ip -4 route replace default dev oflux0 table 201 metric 10
ip -4 route replace unreachable default table 201 metric 32760
ip -4 rule del priority 43 from 198.19.0.1/32 lookup 201 2>/dev/null || true
ip -4 rule add priority 43 from 198.19.0.1/32 lookup 201
ip -4 rule del priority 40 iif br-lan lookup 201 2>/dev/null || true
ip -4 rule del priority 41 to 1.1.1.1/32 lookup 201 2>/dev/null || true
ip -4 rule del priority 42 to 1.0.0.1/32 lookup 201 2>/dev/null || true
ip -4 rule add priority 40 iif br-lan lookup 201
ip -4 rule add priority 41 to 1.1.1.1/32 lookup 201
ip -4 rule add priority 42 to 1.0.0.1/32 lookup 201
logger -t openflux 'LAN and DNS policy enabled through oflux0 (table 201)'
