#!/bin/sh
# Установка xray-client-balancer на узел, где стоит панель 3x-ui.
#
#   ./install.sh              поставить или обновить код, зависимости, обёртку и юнит
#   ./install.sh --start      то же и сразу включить службу (нужен токен в env)
#   ./install.sh --src DIR    взять код из DIR (по умолчанию — каталог этого скрипта)
#
# Скрипт идемпотентный: повторный запуск обновляет код и не трогает ни config.yaml,
# ни env с токеном, ни state.db с назначениями.
set -eu

DEST=/opt/xray-client-balancer
CONF_DIR=/etc/xray-client-balancer
LIB_DIR=/var/lib/xray-client-balancer
UNIT=/etc/systemd/system/xray-client-balancer.service
START=0
SRC=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --start) START=1 ;;
        --src)
            shift
            [ $# -gt 0 ] || { echo "после --src нужен каталог" >&2; exit 2; }
            SRC=$1
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "неизвестный ключ: $1 (см. --help)" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Запускать нужно от root: нужны /opt, /etc и systemd." >&2
    exit 1
fi
[ -f "$SRC/pyproject.toml" ] || {
    echo "В $SRC нет pyproject.toml — это не каталог с кодом (укажите --src)." >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "Нет python3 в PATH." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "Нужен python3 >= 3.11, а на узле $(python3 -V 2>&1)." >&2
    exit 1
}

echo "Каталоги"
install -d -m 0755 "$DEST"
install -d -m 0700 "$CONF_DIR"
install -d -m 0700 "$LIB_DIR" "$LIB_DIR/backups"

if [ "$SRC" != "$DEST" ]; then
    echo "Код: $SRC -> $DEST"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
              --exclude '.pytest_cache' --exclude '*.egg-info' "$SRC"/ "$DEST"/
    else
        tar -C "$SRC" --exclude=.venv --exclude=.git --exclude=__pycache__ \
            --exclude=.pytest_cache --exclude='*.egg-info' -cf - . | tar -C "$DEST" -xf -
    fi
else
    echo "Код уже на месте: $DEST"
fi

echo "Обёртка xcb и systemd-юнит"
install -m 0755 "$DEST/tools/xcb.sh" /usr/local/bin/xcb
install -m 0644 "$DEST/systemd/xray-client-balancer.service" "$UNIT"
systemctl daemon-reload

echo "Зависимости (httpx, pydantic, PyYAML)"
deps_ok=0
if python3 -m venv "$DEST/.venv" >/dev/null 2>&1 && [ -x "$DEST/.venv/bin/python" ]; then
    "$DEST/.venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    if "$DEST/.venv/bin/python" -m pip install --quiet -e "$DEST"; then
        deps_ok=1
        echo "  venv: $DEST/.venv"
    fi
fi
if [ "$deps_ok" -eq 0 ]; then
    # от неудачного `python3 -m venv` остаётся половина каталога
    rm -rf "$DEST/.venv"
    if PYTHONPATH="$DEST/src" python3 -c 'import httpx, pydantic, yaml' >/dev/null 2>&1; then
        deps_ok=1
        echo "  уже есть в системном python3 — ставить нечего"
    fi
fi
if [ "$deps_ok" -eq 0 ] && python3 -m pip --version >/dev/null 2>&1; then
    if python3 -m pip install --quiet --break-system-packages --target "$DEST/deps" httpx pydantic PyYAML; then
        deps_ok=1
        echo "  без venv: $DEST/deps"
    fi
fi
if [ "$deps_ok" -eq 0 ]; then
    echo "Не удалось поставить зависимости: в python3 нет ни venv, ни pip." >&2
    echo "Поставьте пакеты и повторите:  apt install -y python3-venv python3-pip" >&2
    exit 1
fi

if [ ! -f "$CONF_DIR/env" ]; then
    umask 077
    cat > "$CONF_DIR/env" <<'ENVEOF'
# Токен API панели 3x-ui: Настройки -> Безопасность -> API-токены (создать новый).
XRAY_BALANCER_API_TOKEN=
# URL панели нужен только если сервис стоит НЕ на том же узле, что панель:
# XRAY_BALANCER_PANEL_URL=https://127.0.0.1:21868/base-path/
ENVEOF
    chmod 600 "$CONF_DIR/env"
    echo "  создан $CONF_DIR/env"
fi

echo
if xcb --version >/dev/null 2>&1; then
    echo "Установка завершена: $(xcb --version)."
else
    echo "ВНИМАНИЕ: xcb не запускается, проверьте зависимости: xcb --version" >&2
fi

if ! grep -q '^XRAY_BALANCER_API_TOKEN=...*' "$CONF_DIR/env"; then
    echo
    echo "Осталось два шага:"
    echo "  1) впишите токен:  nano $CONF_DIR/env"
    echo "  2) настройте:      xcb init"
elif [ "$START" -eq 1 ]; then
    echo
    systemctl enable --now xray-client-balancer
    echo "Служба включена и запущена."
else
    echo
    echo "Дальше: xcb init && systemctl enable --now xray-client-balancer"
fi
