#!/bin/sh
set -eu
umask 077
check_tun() {
  /etc/openflux/check-tun.sh
}
check_link() {
  /usr/bin/sing-box check -c /etc/openflux/gateway.json || return 1
  ip -4 route show table 200 | grep -q '^default dev vkturn ' || return 1
  /etc/init.d/vkturn running || return 1
  result=$(curl -4 -fsS --socks5-hostname 127.0.0.1:1080 --connect-timeout 15 --max-time 35 https://api.ipify.org) || {
    echo 'SOCKS HTTPS check failed.'
    return 1
  }
  [ "$result" = '37.252.5.64' ] || { echo 'Unexpected OpenFlux exit; LAN not switched.'; return 1; }
  check_tun || return 1
  echo 'OpenFlux SOCKS and TUN checks: 37.252.5.64; VK TURN fallback ready.'
}
if [ "${1:-}" = '--check' ]; then
  check_link
  exit
fi
if ! mkdir /tmp/openflux-transition.lock 2>/dev/null; then
  echo 'Another transition is pending. Wait for its rollback or confirmation.'
  exit 1
fi
confirmed=0
switched=0
started=0
state=''
cleanup() {
  status=$?
  trap - EXIT INT TERM HUP
  if [ "$switched" = 1 ] && [ "$confirmed" = 0 ]; then
    /etc/openflux/restore-vkturn.sh || true
    echo 'VK TURN routing restored.'
  elif [ "$started" = 1 ] && [ "$confirmed" = 0 ]; then
    /etc/init.d/openflux stop || true
  fi
  if [ -n "$state" ]; then touch "$state/finished"; fi
  rmdir /tmp/openflux-transition.lock 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
echo 'OpenFlux activation r2.2: TUN preflight and guarded rollback.'
echo 'Starting OpenFlux separately; VK TURN stays running.'
started=1
/etc/init.d/openflux start
ready=0
consecutive=0
echo 'Waiting for three consecutive SOCKS and TUN checks; LAN stays on VK TURN.'
for attempt in $(seq 1 12); do
  if check_link >/tmp/openflux-activation-check.log 2>&1; then
    consecutive=$((consecutive + 1))
    echo "Preflight passed: $consecutive/3"
    if [ "$consecutive" -ge 3 ]; then ready=1; break; fi
  else
    consecutive=0
  fi
  sleep 2
done
[ "$ready" = 1 ] || {
  cat /tmp/openflux-activation-check.log
  echo 'OpenFlux not ready. LAN remains on VK TURN.'
  exit 1
}
state=$(mktemp -d /tmp/openflux-cutover.XXXXXX)
deadline=$(($(date +%s) + 180))
sh -c '
  trap "" HUP
  sleep 180
  if [ ! -f "$1/finished" ]; then
    touch "$1/rolled-back"
    /etc/openflux/restore-vkturn.sh
    logger -t openflux "No confirmation; VK TURN routing restored"
    rmdir /tmp/openflux-transition.lock 2>/dev/null || true
  fi
' guard "$state" >/tmp/openflux-transition-guard.log 2>&1 </dev/null &
switched=1
/etc/openflux/route-up.sh
ready=0
for attempt in 1 2; do
  echo "Checking TUN after switching: attempt $attempt/2"
  if check_tun >/tmp/openflux-activation-tun.log 2>&1; then ready=1; break; fi
  cat /tmp/openflux-activation-tun.log
  sleep 2
done
[ "$ready" = 1 ] || { echo 'TUN failed after switching; rolling back.'; exit 1; }
if ! nslookup example.com 192.168.1.1 >/tmp/openflux-activation-dns.log 2>&1; then
  echo 'LAN DNS check failed; rolling back.'
  exit 1
fi
confirm_timeout=$((deadline - $(date +%s) - 5))
[ "$confirm_timeout" -gt 0 ] || { echo 'Cutover deadline reached.'; exit 1; }
if [ "$confirm_timeout" -gt 120 ]; then confirm_timeout=120; fi
echo
echo 'LAN and DNS now use OpenFlux. Expected public IP: 37.252.5.64.'
echo 'Check websites on a computer or phone connected to this router.'
echo "Type YES and press Enter within $confirm_timeout seconds to keep it and enable autostart."
echo 'Any other answer, timeout or interrupted SSH restores VK TURN.'
printf 'Keep OpenFlux as primary? '
answer=''
if read -r -t "$confirm_timeout" answer && [ "$answer" = 'YES' ] && [ ! -f "$state/rolled-back" ]; then
  ip -4 rule show | grep -q '^40:.*iif br-lan lookup 201'
  touch /etc/openflux/route-enabled
  /etc/init.d/openflux enable
  confirmed=1
  touch "$state/finished"
  echo 'OpenFlux is primary and starts automatically. VK TURN is retained.'
else
  echo 'Transition not confirmed.'
  exit 1
fi
