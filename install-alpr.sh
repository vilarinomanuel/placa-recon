#!/usr/bin/env bash
# Instalador de alpr_stream.py como servicio systemd.
# Uso:  sudo ./install-alpr.sh [instancia]     (por defecto: entrada)
set -euo pipefail

INSTANCE="${1:-entrada}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/alpr
STATE_DIR=/var/lib/alpr
CONF_DIR=/etc/alpr

[[ $EUID -eq 0 ]] || { echo "Ejecuta con sudo." >&2; exit 1; }

echo "==> Usuario de servicio"
id -u alpr &>/dev/null || useradd --system --home "$STATE_DIR" --shell /usr/sbin/nologin alpr
# Acceso a webcam local (inofensivo si solo usas RTSP)
getent group video >/dev/null && usermod -aG video alpr || true

echo "==> Directorios"
install -d -m 0755 -o root  -g root "$APP_DIR"
install -d -m 0750 -o alpr  -g alpr "$STATE_DIR" "$STATE_DIR/video" "$STATE_DIR/crops" "$STATE_DIR/.cache"
install -d -m 0750 -o root  -g alpr "$CONF_DIR"

echo "==> Entorno Python"
command -v python3 >/dev/null || { echo "Falta python3" >&2; exit 1; }
python3 -m venv "$APP_DIR/venv" 2>/dev/null || true
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet fast-alpr "opencv-python-headless>=4.8"

echo "==> Aplicación"
install -m 0755 -o root -g root "$SRC_DIR/alpr_stream.py" "$APP_DIR/alpr_stream.py"

echo "==> Configuración (0640 root:alpr — contiene credenciales)"
if [[ ! -f "$CONF_DIR/alpr.env" ]]; then
  install -m 0640 -o root -g alpr "$SRC_DIR/alpr.env" "$CONF_DIR/alpr.env"
  echo "    Creado $CONF_DIR/alpr.env — EDITA ALPR_INPUT con tu URL RTSP real."
else
  echo "    $CONF_DIR/alpr.env ya existe; no se sobrescribe."
fi
chmod 0640 "$CONF_DIR"/*.env; chown root:alpr "$CONF_DIR"/*.env

echo "==> Unidad systemd"
install -m 0644 -o root -g root "$SRC_DIR/alpr-stream@.service" /etc/systemd/system/alpr-stream@.service
systemctl daemon-reload
systemctl enable "alpr-stream@${INSTANCE}.service"

cat <<EOF

Listo. Pasos finales:

  sudoedit $CONF_DIR/alpr.env            # coloca tu URL RTSP
  systemctl start alpr-stream@${INSTANCE}
  systemctl status alpr-stream@${INSTANCE}
  journalctl -u alpr-stream@${INSTANCE} -f

Resultados en: $STATE_DIR (placas.csv, crops/, video/)
EOF
