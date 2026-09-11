# placa-recon

Reconocimiento de placas vehiculares (ALPR) sobre archivos de video y cámaras RTSP en tiempo real, con [fast-alpr](https://github.com/ankandrew/fast-alpr) y OpenCV.

Pensado para operar 24/7 como servicio systemd: registra cada matrícula en CSV, guarda una imagen JPEG por detección y graba el video anotado en segmentos.

## Características

- **Archivo o tiempo real** — MP4/AVI, RTSP, RTMP, SRT, HTTP o webcam. El modo se autodetecta por el esquema de la URL.
- **Baja latencia en RTSP** — lector en hilo aparte que conserva solo el frame más reciente, evitando el retardo acumulado del búfer de OpenCV.
- **Reconexión automática** — backoff exponencial de 1 s a 30 s ante caídas del stream, con grabación opcional durante el corte.
- **Registro CSV** — placa, frame, timestamp de stream y de reloj, confianzas, bounding box y rutas de las imágenes. Escritura con flush por fila: resistente a cortes.
- **Anti-duplicados** — ventana configurable (5 s por defecto) para no repetir la misma placa.
- **Imágenes individuales** — un JPEG por placa con margen de contexto, organizado por fecha, y opcionalmente el frame completo como evidencia.
- **Video anotado** — bounding box, texto con confianza y HUD con fecha/hora, con rotación por minutos.
- **Configuración por entorno** — toda opción admite `ALPR_*`, para que la URL RTSP con credenciales nunca aparezca en `ps` ni en el historial del shell.
- **Servicio endurecido** — unidad systemd instanciada con `Restart=always`, usuario sin privilegios y límites de CPU/RAM.

## Requisitos

- Python 3.10 o superior
- `fast-alpr` y `opencv-python` (o `opencv-python-headless` en servidor)
- FFmpeg con soporte RTSP (incluido en las ruedas de OpenCV)

```bash
pip install fast-alpr opencv-python-headless
```

## Uso rápido

```bash
# Archivo de video
python alpr_stream.py -i entrada.mp4 -o salida.mp4 -c placas.csv --save-crops

# Cámara RTSP en tiempo real, 5 fps de inferencia, segmentos de 10 min
python alpr_stream.py -i rtsp://usuario:clave@192.168.1.50:554/Streaming/Channels/101 \
    -o rec/acceso.mp4 -c placas.csv --target-fps 5 --segment-minutes 10 \
    --save-crops --save-full-frame --hud --keep-recording-offline

# Solo CSV e imágenes, sin grabar video
python alpr_stream.py -i 0 --no-video --save-crops
```

Toda opción tiene su variable de entorno equivalente (`ALPR_INPUT`, `ALPR_MIN_CONFIDENCE`, `ALPR_SAVE_CROPS`, ...). Precedencia: argumento CLI > variable de entorno > valor por defecto. Consulta `python alpr_stream.py --help`.

## Instalación como servicio

```bash
sudo ./install-alpr.sh entrada
sudoedit /etc/alpr/alpr.env          # coloca aquí tu URL RTSP real
sudo systemctl start alpr-stream@entrada
journalctl -u alpr-stream@entrada -f
```

El instalador crea el usuario de sistema `alpr`, un venv en `/opt/alpr`, la estructura de datos en `/var/lib/alpr` y copia la plantilla a `/etc/alpr/alpr.env` con permisos `0640 root:alpr`.

La unidad es una plantilla instanciada: una cámara por instancia, cada una con su `/etc/alpr/<instancia>.env` opcional.

```bash
sudo cp alpr.env.example /etc/alpr/estacionamiento.env   # ajusta ALPR_INPUT y rutas
sudo chmod 0640 /etc/alpr/estacionamiento.env
sudo systemctl enable --now alpr-stream@estacionamiento
```

## Seguridad de credenciales

- **`alpr.env.example` es solo una plantilla.** El archivo real vive en `/etc/alpr/`, nunca en el repositorio.
- `.gitignore` bloquea `*.env` (con excepción explícita de la plantilla), claves, certificados, CSV e imágenes, para que una detección real o una contraseña no acaben en un commit.
- La configuración se pasa por `EnvironmentFile`, no por argumentos: la URL RTSP no queda visible en `ps aux` ni en el `ExecStart` de la unidad.
- Las credenciales de la URL se enmascaran en logs y CSV (`rtsp://admin:***@192.168.1.50:554/s1`).
- Si la contraseña de la cámara contiene `@ : / ? # %`, codifícala en percent-encoding (`@` → `%40`, `:` → `%3A`).

## Opciones principales

| Opción | Entorno | Por defecto | Descripción |
|---|---|---|---|
| `-i, --input` | `ALPR_INPUT` | — | Archivo, URL RTSP/HTTP o índice de cámara |
| `-o, --output` | `ALPR_OUTPUT` | `output_annotated.mp4` | Video anotado de salida |
| `-c, --csv` | `ALPR_CSV` | `plates_log.csv` | CSV de detecciones (modo append) |
| `--min-confidence` | `ALPR_MIN_CONFIDENCE` | `0.8` | Umbral estricto de confianza |
| `--dedup-window` | `ALPR_DEDUP_WINDOW` | `5.0` | Ventana anti-duplicados en segundos |
| `--target-fps` | `ALPR_TARGET_FPS` | `0` | Límite de inferencias por segundo en vivo |
| `--frame-skip` | `ALPR_FRAME_SKIP` | `1` | Inferir 1 de cada N frames (archivo) |
| `--save-crops` | `ALPR_SAVE_CROPS` | `false` | Guardar un JPEG por placa |
| `--crops-dir` | `ALPR_CROPS_DIR` | `crops` | Directorio raíz de recortes |
| `--crop-margin` | `ALPR_CROP_MARGIN` | `0.15` | Margen extra alrededor del box |
| `--save-full-frame` | `ALPR_SAVE_FULL_FRAME` | `false` | Guardar también el frame completo |
| `--segment-minutes` | `ALPR_SEGMENT_MINUTES` | `0` | Rotar el video cada N minutos |
| `--rtsp-transport` | `ALPR_RTSP_TRANSPORT` | `tcp` | Transporte RTSP (`tcp` o `udp`) |
| `--keep-recording-offline` | `ALPR_KEEP_RECORDING_OFFLINE` | `false` | Grabar durante la reconexión |
| `--hud` | `ALPR_HUD` | `false` | Superponer contadores y fecha/hora |
| `--no-video` | `ALPR_NO_VIDEO` | `false` | Solo CSV e imágenes |

## Salidas

### CSV

Columnas: `plate`, `frame_id`, `crop_path`, `frame_path`, `stream_timestamp_s`, `stream_timestamp_hms`, `wallclock_local`, `wallclock_utc`, `source`, `ocr_confidence`, `detection_confidence`, `x1`, `y1`, `x2`, `y2`.

```csv
plate,frame_id,crop_path,frame_path,stream_timestamp_s,...
AB123CD,51,crops/2026-09-11/143210-878_AB123CD_091_f51.jpg,,5.000,...
```

### Imágenes

```
crops/
└── 2026-09-11/
    ├── 143210-878_AB123CD_091_f51.jpg        # recorte de la placa
    └── frames/
        └── 143210-878_AB123CD_091_f51_full.jpg   # frame completo
```

Formato del nombre: `<HHMMSS-mmm>_<PLACA>_<confianza×100>_f<frame>.jpg`.

## Estructura del repositorio

| Archivo | Descripción |
|---|---|
| `alpr_stream.py` | Script principal: captura, detección, CSV, recortes y video anotado |
| `alpr.env.example` | Plantilla de configuración (`EnvironmentFile` de systemd) |
| `alpr-stream@.service` | Unidad systemd instanciada con `Restart=always` |
| `install-alpr.sh` | Instalador: usuario, venv, directorios y permisos |

## Notas operativas

- Si el MP4 de salida queda vacío, tu build de OpenCV no trae el codec: usa `--fourcc XVID` con extensión `.avi`.
- En CPU modesta, `--target-fps 5` suele bastar para control de accesos y evita saturar el equipo.
- Para grabación permanente conviene una política de retención (por ejemplo un `systemd-tmpfiles` o un timer que borre recortes y segmentos con más de N días).
- Ajusta `--min-confidence` según la cámara: ángulos forzados y noche degradan el OCR.
- El tratamiento de matrículas puede constituir dato personal según la jurisdicción (RGPD y equivalentes). Define retención, base legal y control de acceso antes de desplegar en producción.

## Licencia

MIT — ver [LICENSE](LICENSE).
