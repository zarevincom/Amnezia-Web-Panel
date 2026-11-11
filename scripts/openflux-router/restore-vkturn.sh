#!/bin/sh
set -eu
rm -f /etc/openflux/route-enabled
/etc/openflux/route-down.sh
/etc/init.d/openflux disable
/etc/init.d/openflux stop
/etc/init.d/vkturn enable
if ! /etc/init.d/vkturn running; then
  /etc/init.d/vkturn start
fi
if ! ip link show vkturn >/dev/null 2>&1; then
  ifup vkturn
fi
logger -t openflux 'Returned LAN routing to existing VK TURN setup'
