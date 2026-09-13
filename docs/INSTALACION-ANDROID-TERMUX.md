# Instalación en Android con Termux + Debian (proot-distro)

Guía paso a paso para ejecutar `placa-recon` en un teléfono Android sin root, usando Termux
como anfitrión y Debian dentro de `proot-distro` para las dependencias de Python.

Probado sobre Android 10+ / arm64 (aarch64). Requiere unos 3 GB libres y, para el panel web,
ningún puerto privilegiado.

---

## 0. Arquitectura de la instalación

```
Android
├── Termux (anfitrión)
│   ├── termux-api      → cámara del teléfono, wake-lock, notificaciones
│   └── proot-distro
│        └── Debian (invitado)
│             ├── Python 3 + venv en /opt/alpr
│             ├── alpr_stream.py  (motor ALPR)
│             └── web/web_api.py  (panel, puerto 8080)
└── App de cámara IP (opcional) → flujo RTSP local que consume el ALPR
```

Punto clave: **dentro de proot no existen `/dev/video*`**. Android no expone la cámara como
dispositivo V4L2, así que el motor no puede abrir la cámara directamente desde Debian. Las tres
vías válidas están en el paso 6.

---

## 1. Instalar Termux (no desde Play Store)

La versión de Play Store está congelada y sus paquetes fallan. Descarga desde
[F-Droid](https://f-droid.org/packages/com.termux/) o
[los releases de GitHub](https://github.com/termux/termux-app/releases):

1. `Termux` (app principal).
2. `Termux:API` (addon; imprescindible si vas a usar la cámara del teléfono).

Ambos deben instalarse **desde la misma fuente**, o Android rechazará el addon por firma distinta.

## 2. Preparar Termux

```bash
pkg update && pkg upgrade -y
pkg install -y proot-distro git termux-api openssl
termux-setup-storage        # concede acceso a /sdcard (para videos y salidas)
```

Comprueba el addon de API y la cámara trasera (ID 0 en casi todos los equipos):

```bash
termux-camera-info | head -40
```

Evita que Android mate el proceso durante el reconocimiento:

```bash
termux-wake-lock
```

Además, en Ajustes de Android → Aplicaciones → Termux → Batería, elige **Sin restricciones**.

## 3. Instalar Debian dentro de Termux

```bash
proot-distro install debian
proot-distro login debian
```

Desde aquí el prompt es `root@localhost` y ya estás **dentro de Debian**. Para volver a Termux:
`exit`. Para entrar de nuevo compartiendo el almacenamiento del teléfono:

```bash
proot-distro login debian --bind /sdcard:/sdcard
```

## 4. Dependencias del sistema en Debian

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git \
               libgl1 libglib2.0-0 ffmpeg curl nano
```

`libgl1` y `libglib2.0-0` son las bibliotecas que OpenCV carga en tiempo de ejecución; sin ellas
el `import cv2` falla con `libGL.so.1: cannot open shared object file`.

## 5. Clonar el proyecto y crear el entorno

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/vilarinomanuel/placa-recon.git alpr
cd /opt/alpr

python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip wheel
```

Instala las dependencias. `fast-alpr` **no instala ningún runtime ONNX por defecto**: hay que
pedir el extra explícitamente ([PyPI de fast-alpr](https://pypi.org/project/fast-alpr/)):

```bash
pip install "fast-alpr[onnx]" opencv-python-headless
```

Notas para aarch64:

- Usa siempre `opencv-python-headless` ([PyPI](https://pypi.org/project/opencv-python-headless/)):
  no hay servidor gráfico y la variante completa arrastra GTK innecesario.
- Si `pip` intenta compilar OpenCV desde fuente (tarda horas en un teléfono), corta con Ctrl-C y
  usa el paquete de Debian: `apt install -y python3-opencv` y crea el venv con
  `python3 -m venv --system-site-packages venv`.
- Si `onnxruntime` no encuentra rueda para tu combinación de Python y arquitectura, fija una
  versión anterior (`pip install "onnxruntime==1.19.2"`) o instala Python 3.11 en Debian, que es
  la que más ruedas aarch64 tiene publicadas.

Verificación rápida:

```bash
python -c "import cv2, onnxruntime; print(cv2.__version__, onnxruntime.get_available_providers())"
```

La primera ejecución del ALPR descarga los modelos ONNX (unos 100 MB) a la caché del usuario;
hazla con Wi-Fi.

## 6. Elegir la fuente de video

### Opción A — Cámara IP en el propio teléfono (recomendada)

Instala una app de cámara IP que publique RTSP (por ejemplo *IP Webcam* o *RTSP Camera Server*),
arráncala y anota la URL. Como el flujo viaja por el loopback del teléfono, la latencia es mínima:

```bash
export ALPR_INPUT="rtsp://127.0.0.1:8554/live"
```

Es la única vía que da video continuo en tiempo real con el teléfono como cámara.

### Opción B — Video ya grabado

```bash
proot-distro login debian --bind /sdcard:/sdcard    # desde Termux
export ALPR_INPUT="/sdcard/Movies/acceso.mp4"
```

### Opción C — Fotogramas de la cámara nativa vía Termux:API

Desde **Termux** (no desde Debian), captura periódicamente y deja las imágenes en una carpeta
compartida:

```bash
mkdir -p ~/capturas
while true; do
  termux-camera-photo -c 0 ~/capturas/$(date +%s).jpg
  sleep 2
done
```

`-c 0` es la cámara trasera ([wiki de Termux](https://wiki.termux.com/wiki/Termux-camera-photo)).
Es un muestreo, no un flujo: sirve para control de accesos lento, no para tráfico en movimiento.

### Sobre la rutina automática de selección de cámara

`video_source.py` detecta Android y prioriza la cámara trasera, pero solo cuando encuentra
`termux-camera-info` en el `PATH`, es decir ejecutándose **desde Termux**. Dentro de proot ese
binario no existe, así que la selección interactiva ofrecerá archivo o RTSP. Es el comportamiento
esperado y por eso en el servicio se fija siempre `ALPR_INPUT`.

## 7. Configuración

```bash
cd /opt/alpr
cp alpr.env.example alpr.env
chmod 600 alpr.env
nano alpr.env
```

Valores razonables para un teléfono:

```ini
ALPR_INPUT=rtsp://127.0.0.1:8554/live
ALPR_OUTPUT_DIR=/sdcard/alpr           # sobrevive a reinstalar Debian
ALPR_CSV=/sdcard/alpr/placas.csv
ALPR_MIN_CONFIDENCE=0.80
ALPR_TARGET_FPS=3                      # clave: no saturar la CPU del móvil
ALPR_DEDUPE_SECONDS=5
ALPR_SAVE_CROPS=true
ALPR_SAVE_VIDEO=false                  # el encoder consume mucha batería
ALPR_FOURCC=XVID
```

Baja `ALPR_TARGET_FPS` antes que cualquier otra cosa si el teléfono se calienta. Con
`ALPR_SAVE_VIDEO=false` el consumo cae bastante y sigues teniendo CSV y recortes por placa.

## 8. Primera ejecución

```bash
cd /opt/alpr && . venv/bin/activate
set -a && . ./alpr.env && set +a
python alpr_stream.py -v
```

Deberías ver el log de apertura de la fuente, el FPS efectivo y una línea por placa registrada.
Corta con Ctrl-C: el CSV se escribe con flush por fila, así que no se pierde nada.

## 9. Panel web en el teléfono

```bash
pip install fastapi uvicorn
ALPR_WEB_DATA=/sdcard/alpr python web/web_api.py --host 127.0.0.1 --port 8080
```

Abre `http://127.0.0.1:8080` en el navegador del propio teléfono. Para verlo desde el PC en la
misma red usa `--host 0.0.0.0` y la IP del móvil (`ip a` en Termux), **solo en red de confianza**:
el panel muestra matrículas y no trae autenticación propia.

Para probar la interfaz sin cámara ni detecciones reales:

```bash
python web/generar_demo.py --horas 48 --detecciones 420
python web/web_api.py --demo
```

## 10. Arranque automático (sin systemd)

Dentro de proot no hay systemd, así que `alpr-stream@.service` e `install-alpr.sh` no aplican en
Android. Usa el mecanismo de arranque de Termux:

```bash
# En Termux
pkg install -y termux-services
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/alpr <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
proot-distro login debian --bind /sdcard:/sdcard -- \
  /bin/bash -lc 'cd /opt/alpr && . venv/bin/activate && set -a && . ./alpr.env && set +a && \
  exec python alpr_stream.py >> /sdcard/alpr/alpr.log 2>&1'
EOF
chmod +x ~/.termux/boot/alpr
```

Requiere el addon **Termux:Boot** (F-Droid). Para reinicio automático ante fallos, envuelve la
llamada en un bucle:

```sh
while true; do  <comando> ; sleep 5; done
```

que es el equivalente práctico de `Restart=always`.

## 11. Mantenimiento

```bash
# Actualizar el proyecto
cd /opt/alpr && git pull && . venv/bin/activate && pip install -U "fast-alpr[onnx]"

# Espacio ocupado por recortes
du -sh /sdcard/alpr/crops

# Purgar recortes de más de 14 días
find /sdcard/alpr/crops -type f -mtime +14 -delete

# Copia de respaldo del CSV
cp /sdcard/alpr/placas.csv /sdcard/Download/placas-$(date +%F).csv
```

## Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `libGL.so.1: cannot open shared object file` | Falta `libgl1` en Debian: `apt install -y libgl1 libglib2.0-0` |
| `pip` empieza a compilar OpenCV | No hay rueda para tu Python: usa `apt install python3-opencv` + venv con `--system-site-packages` |
| `No matching distribution found for onnxruntime` | Fija `onnxruntime==1.19.2` o usa Python 3.11 |
| No abre la cámara desde Debian | Comportamiento normal: Android no expone `/dev/video*` en proot. Usa RTSP (opción A) |
| `termux-camera-info` no responde | Falta la app Termux:API, o no se concedió el permiso de cámara a Termux |
| El proceso muere al bloquear la pantalla | Falta `termux-wake-lock` y/o la optimización de batería sigue activa |
| MP4 de salida vacío | Codec ausente: `ALPR_FOURCC=XVID` y extensión `.avi` |
| El teléfono se calienta y el FPS cae | Baja `ALPR_TARGET_FPS` a 2–3 y pon `ALPR_SAVE_VIDEO=false` |

## Expectativas de rendimiento

En un teléfono de gama media (Snapdragon 6xx/7xx, CPU en proot, sin NNAPI) el pipeline completo
—detección + OCR— rinde del orden de 1–4 fotogramas por segundo. Es suficiente para control de
accesos con barrera o vehículos a paso de hombre; no para tráfico a velocidad de vía. Los modelos
ONNX corren en CPU: la NPU del teléfono no se usa.

## Aviso legal

Las matrículas son dato personal en muchas jurisdicciones (RGPD y equivalentes). Define retención,
base legal y control de acceso antes de desplegar, y no expongas el panel a Internet.
