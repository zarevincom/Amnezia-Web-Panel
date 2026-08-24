#!/usr/bin/env bash
#
# Amnezia-Web-Panel — обновление установки на сервере (в т.ч. до версии с AIVPN).
#
# Запускать НА VPS, из каталога с исходниками панели:
#     cd /opt/amnezia-web-panel && bash deploy-update.sh
#
# Что делает:
#   1. Определяет способ установки (docker compose / docker run / systemd / bare).
#   2. Снимает резервную копию data.json ДО любых изменений.
#      Это обязательный шаг: штатный docker-compose.yml монтирует том в
#      /app/data, а приложение пишет в /app/data.json — то есть данные панели
#      живут в перезаписываемом слое контейнера и пропадают при пересборке.
#   3. Пересобирает/перезапускает панель из локальных исходников.
#   4. Возвращает data.json на место и проверяет, что панель отвечает.
#
# Скрипт идемпотентен и на каждом шаге останавливается при ошибке.

set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${APP_DIR}/.update-backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONTAINER="${PANEL_CONTAINER:-amnezia_panel}"
PORT="${APP_PORT:-5000}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

trap 'die "Прервано на строке $LINENO. Резервные копии: ${BACKUP_DIR}"' ERR

mkdir -p "$BACKUP_DIR"

# ─── 1. Определяем режим установки ───────────────────────────────────────────
MODE="unknown"
if command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    if [ -f "${APP_DIR}/docker-compose.yml" ]; then
        MODE="compose"
    else
        MODE="docker"
    fi
elif systemctl list-units --type=service --all 2>/dev/null | grep -q 'amnezia.*panel'; then
    MODE="systemd"
elif pgrep -f "python.*app\.py" >/dev/null 2>&1; then
    MODE="bare"
fi
log "Режим установки: ${MODE}"
[ "$MODE" = "unknown" ] && warn "Не удалось определить автоматически — будет выполнена только сборка."

# docker compose v2 или v1
compose() {
    if docker compose version >/dev/null 2>&1; then docker compose "$@"
    else docker-compose "$@"; fi
}

# ─── 2. Резервная копия данных ───────────────────────────────────────────────
BACKUP_DATA="${BACKUP_DIR}/data.json.${STAMP}"
RESTORED_FROM=""

if [ "$MODE" = "compose" ] || [ "$MODE" = "docker" ]; then
    if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        # Пробуем оба возможных места, начиная с фактического.
        for p in /app/data.json /app/data/data.json; do
            if docker exec "$CONTAINER" test -f "$p" 2>/dev/null; then
                docker cp "${CONTAINER}:${p}" "$BACKUP_DATA"
                RESTORED_FROM="$p"
                log "data.json сохранён из контейнера (${p}) → ${BACKUP_DATA}"
                break
            fi
        done
        [ -z "$RESTORED_FROM" ] && warn "data.json в контейнере не найден — возможно, панель ещё не настраивалась."
    else
        warn "Контейнер ${CONTAINER} не запущен, данные из него снять нельзя."
    fi
fi

if [ -z "$RESTORED_FROM" ] && [ -f "${APP_DIR}/data.json" ]; then
    cp -a "${APP_DIR}/data.json" "$BACKUP_DATA"
    RESTORED_FROM="${APP_DIR}/data.json"
    log "data.json сохранён с диска → ${BACKUP_DATA}"
fi

if [ -n "$RESTORED_FROM" ]; then
    python3 - "$BACKUP_DATA" <<'PY' 2>/dev/null || die "Резервная копия data.json повреждена (невалидный JSON) — обновление остановлено, панель не тронута."
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
print(f"    серверов: {len(d.get('servers', []))}, "
      f"пользователей: {len(d.get('users', []))}, "
      f"подключений: {len(d.get('user_connections', []))}")
PY
fi

# Заодно копия текущего docker-compose.yml — вдруг он правился вручную.
[ -f "${APP_DIR}/docker-compose.yml" ] && \
    cp -a "${APP_DIR}/docker-compose.yml" "${BACKUP_DIR}/docker-compose.yml.${STAMP}"

# ─── 3. Проверка кода перед выкаткой ─────────────────────────────────────────
log "Проверяю синтаксис Python..."
if command -v python3 >/dev/null 2>&1; then
    python3 -m py_compile "${APP_DIR}/app.py" "${APP_DIR}"/managers/*.py \
        || die "Ошибка синтаксиса — выкатка отменена, работающая панель не тронута."
    python3 - <<PY || die "Повреждён файл перевода — выкатка отменена."
import json, glob
for f in glob.glob("${APP_DIR}/translations/*.json"):
    json.load(open(f, encoding='utf-8'))
PY
    log "Код в порядке (включая managers/aivpn_manager.py)."
fi

# ─── 4. Пересборка и перезапуск ──────────────────────────────────────────────
case "$MODE" in
  compose)
    log "Пересобираю образ из локальных исходников..."
    # build: . важен — иначе compose потянет готовый образ из Docker Hub
    # и ваши правки (AIVPN) в контейнер не попадут.
    if ! grep -q 'build:' "${APP_DIR}/docker-compose.yml"; then
        warn "В docker-compose.yml нет секции build: — compose возьмёт образ с Docker Hub."
        warn "Добавьте в сервис amnezia_panel строки:"
        warn "    build: ."
        die  "Иначе обновление с AIVPN не попадёт на сервер."
    fi
    compose build --pull
    log "Перезапускаю..."
    compose up -d --force-recreate
    ;;
  docker)
    # Порт панели и HTTPS живут в data.json (settings.ssl.panel_port), а не в
    # переменных окружения. Поэтому новый контейнер обязан публиковать РОВНО
    # те же порты, что старый — иначе панель поднимется, но станет недоступна
    # по прежнему адресу. Ничего не угадываем: снимаем конфигурацию с
    # работающего контейнера.
    docker inspect "$CONTAINER" > "${BACKUP_DIR}/container-inspect.${STAMP}.json"
    log "Конфигурация контейнера сохранена → ${BACKUP_DIR}/container-inspect.${STAMP}.json"

    OLD_PORTS="$(docker inspect --format \
      '{{range $p, $c := .HostConfig.PortBindings}}{{range $c}}-p {{if .HostIp}}{{.HostIp}}:{{end}}{{.HostPort}}:{{$p}} {{end}}{{end}}' \
      "$CONTAINER")"
    OLD_BINDS="$(docker inspect --format \
      '{{range .HostConfig.Binds}}-v {{.}} {{end}}' "$CONTAINER" 2>/dev/null || true)"
    OLD_VOLS="$(docker inspect --format \
      '{{range .Mounts}}{{if eq .Type "volume"}}-v {{.Name}}:{{.Destination}} {{end}}{{end}}' \
      "$CONTAINER" 2>/dev/null || true)"
    OLD_RESTART="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER")"
    [ -z "$OLD_RESTART" ] || [ "$OLD_RESTART" = "no" ] && OLD_RESTART="unless-stopped"

    [ -z "$OLD_PORTS" ] && die "У контейнера ${CONTAINER} не найдено проброшенных портов — \
останавливаюсь, чтобы не поднять панель на недоступном порту. Проверьте: docker port ${CONTAINER}"

    log "Порты старого контейнера: ${OLD_PORTS}"

    log "Пересобираю образ из локальных исходников..."
    docker build -t amnezia-panel:local "$APP_DIR"

    # Старый контейнер не удаляем, а переименовываем — мгновенный откат,
    # если новый не взлетит.
    OLD_NAME="${CONTAINER}_old_${STAMP}"
    log "Останавливаю старый контейнер и переименовываю в ${OLD_NAME}..."
    docker stop "$CONTAINER" >/dev/null
    docker rename "$CONTAINER" "$OLD_NAME"
    ROLLBACK_NAME="$OLD_NAME"

    log "Запускаю новый контейнер с теми же портами и томами..."
    # shellcheck disable=SC2086
    docker run -d --name "$CONTAINER" --restart "$OLD_RESTART" \
        $OLD_PORTS $OLD_BINDS $OLD_VOLS amnezia-panel:local
    ;;
  systemd)
    UNIT="$(systemctl list-units --type=service --all --plain --no-legend 2>/dev/null \
            | grep -o '^[^ ]*amnezia[^ ]*panel[^ ]*\.service' | head -1)"
    [ -z "$UNIT" ] && die "Не нашёл systemd-юнит панели."
    log "Обновляю зависимости и перезапускаю ${UNIT}..."
    pip3 install -q --no-cache-dir -r "${APP_DIR}/requirements.txt" || \
        warn "pip завершился с ошибкой — проверьте зависимости вручную."
    systemctl restart "$UNIT"
    ;;
  bare)
    warn "Панель запущена вручную (python app.py). Перезапустите процесс сами:"
    warn "    pkill -f 'python.*app.py' && cd ${APP_DIR} && nohup python3 app.py &"
    ;;
esac

# ─── 5. Возвращаем данные ────────────────────────────────────────────────────
if [ -f "$BACKUP_DATA" ] && { [ "$MODE" = "compose" ] || [ "$MODE" = "docker" ]; }; then
    log "Жду запуска контейнера..."
    for _ in $(seq 1 30); do
        docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" && break
        sleep 1
    done
    sleep 3
    TARGET="${RESTORED_FROM:-/app/data.json}"
    docker cp "$BACKUP_DATA" "${CONTAINER}:${TARGET}"
    log "data.json возвращён в контейнер (${TARGET})."
    docker restart "$CONTAINER" >/dev/null
    log "Контейнер перезапущен, чтобы панель перечитала данные."
fi

# ─── 6. Проверка ─────────────────────────────────────────────────────────────
# Проверяем только там, где скрипт действительно перезапускал сервис. В режимах
# bare/unknown перезапуск делает оператор вручную, и ждать здесь нечего.
if [ "$MODE" = "bare" ] || [ "$MODE" = "unknown" ]; then
    echo
    log "Код проверен и готов. Перезапустите панель вручную, затем откройте её в браузере."
    log "Резервные копии: ${BACKUP_DIR}"
    exit 0
fi

# Порт и схему не задаём константой: панель может слушать нестандартный порт
# и работать по HTTPS — и то, и другое настраивается внутри data.json
# (settings.ssl.panel_port / settings.ssl.enabled), а не через окружение.
HEALTH_PORT=""
if [ -f "$BACKUP_DATA" ] && command -v python3 >/dev/null 2>&1; then
    HEALTH_PORT="$(python3 - "$BACKUP_DATA" 2>/dev/null <<'PY' || true
import json, sys
ssl = json.load(open(sys.argv[1], encoding='utf-8')).get('settings', {}).get('ssl', {})
print(ssl.get('panel_port') or '')
PY
)"
fi
# Иначе берём первый опубликованный порт контейнера.
if [ -z "$HEALTH_PORT" ] && command -v docker >/dev/null 2>&1; then
    HEALTH_PORT="$(docker inspect --format \
      '{{range $p, $c := .HostConfig.PortBindings}}{{range $c}}{{.HostPort}} {{end}}{{end}}' \
      "$CONTAINER" 2>/dev/null | awk '{print $1}')"
fi
HEALTH_PORT="${HEALTH_PORT:-$PORT}"

log "Проверяю, что панель отвечает на порту ${HEALTH_PORT}..."
OK=""
for _ in $(seq 1 30); do
    # -k: сертификат может быть самоподписанным или выписанным на внешнее имя,
    # а стучимся мы на 127.0.0.1 — проверяем живость, а не валидность цепочки.
    if curl -fsSk -o /dev/null "https://127.0.0.1:${HEALTH_PORT}/login" 2>/dev/null \
    || curl -fsS  -o /dev/null "http://127.0.0.1:${HEALTH_PORT}/login"  2>/dev/null; then
        OK=1; break
    fi
    sleep 2
done

if [ -n "$OK" ]; then
    log "Готово. Панель отвечает на порту ${HEALTH_PORT} — откройте её по вашему обычному адресу."
    if [ "$MODE" = "compose" ] || [ "$MODE" = "docker" ]; then
        echo
        log "Проверка, что сборка содержит AIVPN:"
        docker exec "$CONTAINER" test -f /app/managers/aivpn_manager.py \
            && echo "    ✓ managers/aivpn_manager.py на месте" \
            || warn "    ✗ aivpn_manager.py отсутствует — собрался старый образ!"
    fi
else
    warn "Панель не ответила за 60 секунд. Логи:"
    case "$MODE" in
      compose) compose logs --tail 50 ;;
      docker)  docker logs --tail 50 "$CONTAINER" ;;
    esac
    echo
    warn "Данные целы: ${BACKUP_DATA}"
    if [ -n "${ROLLBACK_NAME:-}" ]; then
        warn "Откатиться на прежнюю версию одной командой:"
        warn "    docker rm -f ${CONTAINER} && docker rename ${ROLLBACK_NAME} ${CONTAINER} && docker start ${CONTAINER}"
    fi
    die "Обновление завершилось, но панель не поднялась."
fi

# Успех: подсказываем, как убрать страховочный контейнер.
if [ -n "${ROLLBACK_NAME:-}" ]; then
    echo
    log "Прежний контейнер сохранён как ${ROLLBACK_NAME} на случай отката."
    log "Когда убедитесь, что всё работает, удалите его:  docker rm ${ROLLBACK_NAME}"
fi

echo
log "Резервные копии сохранены в ${BACKUP_DIR} (data.json + docker-compose.yml)."
