#!/bin/sh
# Обёртка для узла: кладётся в /usr/local/bin/xcb.
#
# Зачем обёртка, а не прямо `xray-client-balancer`:
#   * токен панели читается из /etc/xray-client-balancer/env (права 600) и не попадает
#     ни в командную строку, ни в историю шелла;
#   * код и зависимости берутся из /opt/xray-client-balancer (venv там может отсутствовать,
#     поэтому PYTHONPATH собирается вручную);
#   * любой ключ после имени команды прокидывается как есть: xcb move user@example 2 --yes
set -eu

ENV_FILE=/etc/xray-client-balancer/env
ROOT=/opt/xray-client-balancer

if [ -r "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

PYTHONPATH="$ROOT/src:$ROOT/deps${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

if [ -x "$ROOT/.venv/bin/python" ]; then
    exec "$ROOT/.venv/bin/python" -m xray_client_balancer.main "$@"
fi
exec python3 -m xray_client_balancer.main "$@"
