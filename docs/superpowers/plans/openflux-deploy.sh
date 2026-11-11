#!/usr/bin/env bash
set -Eeuo pipefail
release=${1:?release SHA required}
[[ $release =~ ^[0-9a-f]{40}$ ]] || exit 2
short=${release:0:7}
root=/opt/amneziavpnphp
stamp=$(date -u +%Y%m%dT%H%M%SZ)
stage="$root/.staging/openflux-$short-$stamp"
backup="$root/backups/pre-openflux-$short-$stamp"
old_source="$root/app-before-openflux-$short-$stamp"
candidate="amnezia-web-panel-revert:openflux-$short"
candidate_container="codex-panel-candidate-$short"
old_image=$(docker inspect -f '{{.Image}}' amnezia-panel-web)
swapped=0
candidate_owned=0
cleanup() {
  if [ "$candidate_owned" = 1 ]; then
    docker rm -f "$candidate_container" >/dev/null 2>&1 || true
    candidate_owned=0
  fi
}
rollback() {
  code=$?
  trap - ERR
  if [ "$swapped" = 1 ]; then
    echo 'Deployment failed; restoring previous source and image.'
    docker image tag "$old_image" amnezia-web-panel-revert:latest
    if [ -d "$root/app" ]; then mv "$root/app" "$stage/failed-app"; fi
    mv "$old_source" "$root/app"
    (cd "$root" && docker compose up -d --no-build --no-deps web)
  fi
  cleanup
  exit "$code"
}
trap rollback ERR
trap cleanup EXIT
cd "$root/app"
# Reject unrecorded production edits before replacing its source.
sha256sum -c <<'SUMS'
e58094ee4d4c57acf9f67b2591717fbe1b2c3cc6a30e92cffc9a32d55b1b8359  app.py
fb7d4c41123f80bf8db7682216d8d40ee6b96fa2c6d10197cbddaf4e79fbd4e1  templates/server.html
87ecb86f9e5e2b703a697a5fbd6ba6be5ecb9d2775d05e844c5a4f7e679aa636  README.md
375c94f839e6a4b339f1a6ba83c2b20aace3cd4531141400ba723aa34c5ad6f2  translations/en.json
56066dbec494752004e95c9553cf0e1131f6be535ae3dc1d17816dd7f33e92cf  translations/ru.json
c74f6e4ab3685e925586f6147fb135e8ee7620485dcdbaa34d0e2ef78012ab2f  translations/fa.json
fd6d3c4082821d5e64fcf8a0794f8cec42384d486c35739e66d4d031ea7eeca3  translations/fr.json
dd1f090829e7c721212cbac0634ca3976ce0c479d9a4b767e2da68f046e36386  translations/zh.json
SUMS
test ! -e managers/openflux_manager.py
mkdir -p "$stage/app" "$backup"
chmod 700 "$stage" "$backup"
tar -xzf "$root/.staging/openflux-$release.tar.gz" -C "$stage/app"
printf '%s\n' "$release" > "$stage/app/.deployed-commit"
echo "Building $candidate"
if ! docker build -t "$candidate" "$stage/app" > "$stage/build.log" 2>&1; then
  tail -40 "$stage/build.log"
  exit 1
fi
echo 'Running test suite in production Python image'
if ! docker run --rm --network none --entrypoint python "$candidate" -m unittest discover -s tests -v > "$stage/tests.log" 2>&1; then
  tail -60 "$stage/tests.log"
  exit 1
fi
tail -5 "$stage/tests.log"
docker inspect "$candidate_container" >/dev/null 2>&1 && { echo 'Candidate container already exists'; exit 1; }
docker run -d --name "$candidate_container" --network none "$candidate" >/dev/null
candidate_owned=1
ready=0
for attempt in $(seq 1 30); do
  if docker exec "$candidate_container" python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:5000/login").status == 200' >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
test "$ready" = 1
cleanup

cd "$root"
cp -a docker-compose.yml Caddyfile .env "$backup/"
if [ -f data.json ]; then cp -a data.json "$backup/"; fi
docker image tag "$old_image" "amnezia-web-panel-revert:rollback-$short-$stamp"
docker exec -i amnezia-panel-web python - "$short-$stamp" <<'PY'
import os, sqlite3, sys
source = sqlite3.connect(os.environ['PANEL_DB_FILE'])
destination = sqlite3.connect('/app/data/panel-before-openflux-' + sys.argv[1] + '.db')
source.backup(destination)
assert destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
destination.close()
source.close()
print('Consistent SQLite backup verified')
PY
docker exec -i amnezia-panel-web python - <<'PY' > "$backup/state-before.json"
import json, app
data = app.load_data()
print(json.dumps({key: len(data.get(key, [])) for key in ('servers','users','user_connections')}))
PY
mv "$root/app" "$old_source"
swapped=1
mv "$stage/app" "$root/app"
docker image tag "$candidate" amnezia-web-panel-revert:latest
docker compose up -d --no-build --no-deps web
ready=0
for attempt in $(seq 1 30); do
  if docker exec amnezia-panel-web python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:5000/login").status == 200' >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
test "$ready" = 1
docker exec -i amnezia-panel-web python - <<'PY' > "$backup/state-after.json"
import json, app
data = app.load_data()
assert 'openflux' in app.BASE_PROTOCOLS
assert len(app.STATE_STORE.plaintext_secret_paths()) == 0
print(json.dumps({key: len(data.get(key, [])) for key in ('servers','users','user_connections')}))
PY
python3 - "$backup/state-before.json" "$backup/state-after.json" <<'PY'
import json, sys
before, after = [json.load(open(path)) for path in sys.argv[1:]]
assert all(after[key] >= value for key, value in before.items()), (before, after)
print('State preserved, secret encryption verified:', after)
PY
docker exec -i amnezia-panel-web python - <<'PY'
import app
from starlette.requests import Request
from unittest.mock import patch
scope = {'type':'http','method':'GET','path':'/server/0','root_path':'',
         'scheme':'http','headers':[], 'query_string':b'',
         'server':('localhost',5000),'client':('127.0.0.1',1)}
request = Request(scope)
request.state.user = {'id':'deployment-check','role':'admin','username':'deployment-check'}
# Call the normal read-only page handler without a login or app startup hooks.
with patch.object(app, 'get_current_user', return_value=request.state.user):
    response = app.server_detail(request, 0).body.decode('utf-8')
assert 'id="proto-openflux"' in response
assert 'id="openfluxModal"' in response
print('Rendered OpenFlux Marketplace and connection-link modal')
PY
printf '%s\n' "$release" > "$root/.deployed-commit"
echo "Deployed $release"
echo "Previous source: $old_source"
echo "Backup: $backup"
docker ps --filter name=amnezia-panel --format '{{.Names}} {{.Status}}'
trap - ERR
