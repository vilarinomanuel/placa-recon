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
# Estructura única de datos (idéntica a la de Windows y a la del panel web).
install -d -m 0750 -o alpr -g alpr "$STATE_DIR" "$STATE_DIR/video" "$STATE_DIR/crops" \
        "$STATE_DIR/live" "$STATE_DIR/logs" "$STATE_DIR/run" "$STATE_DIR/.cache"
install -d -m 0750 -o root  -g alpr "$CONF_DIR"

echo "==> Entorno Python"
command -v python3 >/dev/null || { echo "Falta python3" >&2; exit 1; }
python3 -m venv "$APP_DIR/venv" 2>/dev/null || true
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet "fast-alpr[onnx]" "opencv-python-headless>=4.8" \
    fastapi "uvicorn[standard]"

echo "==> Aplicación"
install -m 0755 -o root -g root "$SRC_DIR/alpr_stream.py"  "$APP_DIR/alpr_stream.py"
install -m 0644 -o root -g root "$SRC_DIR/video_source.py" "$APP_DIR/video_source.py"
# Panel web (opcional): mismo árbol de código que el motor.
if [[ -d "$SRC_DIR/web" ]]; then
  install -d -m 0755 -o root -g root "$APP_DIR/web" "$APP_DIR/web/static"
  install -m 0644 -o root -g root "$SRC_DIR"/web/*.py "$APP_DIR/web/"
  install -m 0644 -o root -g root "$SRC_DIR"/web/static/* "$APP_DIR/web/static/"
fi

echo "==> Configuración (0640 root:alpr — contiene credenciales)"
if [[ ! -f "$CONF_DIR/alpr.env" ]]; then
  install -m 0640 -o root -g alpr "$SRC_DIR/alpr.env.example" "$CONF_DIR/alpr.env"
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

Rutas de la instalación (una sola estructura):
  código   $APP_DIR            (venv en $APP_DIR/venv, panel en $APP_DIR/web)
  config   $CONF_DIR/alpr.env  + $CONF_DIR/<instancia>.env
  datos    $STATE_DIR          placas.csv, camaras.json
           $STATE_DIR/crops    recortes y frames completos
           $STATE_DIR/video    <instancia>.mp4 anotado
           $STATE_DIR/live     <instancia>.jpg de la vista en vivo
           $STATE_DIR/logs     registros del panel y de cada cámara
           $STATE_DIR/run      estado del supervisor de procesos

Panel web (opcional):
  sudo -u alpr ALPR_HOME=$APP_DIR $APP_DIR/venv/bin/python $APP_DIR/web/web_api.py
EOF
