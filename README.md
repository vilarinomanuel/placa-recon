# placa-recon

Reconocimiento de placas vehiculares (ALPR) sobre archivos de video y cámaras RTSP en tiempo real, con [fast-alpr](https://github.com/ankandrew/fast-alpr) y OpenCV.

Pensado para operar 24/7 como servicio systemd: registra cada matrícula en CSV, guarda una imagen JPEG por detección y graba el video anotado en segmentos.

## Características

- **Selección de fuente multiplataforma** — cámara trasera automática en Android; menú de archivo o cámara en vivo en Windows, Linux y macOS.
- **Archivo o tiempo real** — MP4/AVI, RTSP, RTMP, SRT, HTTP o webcam. El modo se autodetecta por el esquema de la URL.
- **Baja latencia en RTSP** — lector en hilo aparte que conserva solo el frame más reciente, evitando el retardo acumulado del búfer de OpenCV.
- **Reconexión automática** — backoff exponencial de 1 s a 30 s ante caídas del stream, con grabación opcional durante el corte.
- **Registro CSV** — placa, frame, timestamp de stream y de reloj, confianzas, bounding box y rutas de las imágenes. Escritura con flush por fila: resistente a cortes.
- **Anti-duplicados** — ventana configurable (5 s por defecto) para no repetir la misma placa.
- **Imágenes individuales** — un JPEG por placa con margen de contexto, organizado por fecha, y opcionalmente el frame completo como evidencia.
- **Video anotado** — bounding box, texto con confianza y HUD con fecha/hora, con rotación por minutos.
- **Configuración por entorno** — toda opción admite `ALPR_*`, para que la URL RTSP con credenciales nunca aparezca en `ps` ni en el historial del shell.
- **Panel web** — interfaz de administración de cámaras y monitorización del ALPR en tiempo real (FastAPI + SPA sin dependencias de build).
- **Servicio endurecido** — unidad systemd instanciada con `Restart=always`, usuario sin privilegios y límites de CPU/RAM.

## Selección de fuente de video

El módulo `video_source.py` detecta la plataforma en runtime y actúa en consecuencia. Es independiente del pipeline: el núcleo de procesamiento recibe una fuente ya resuelta y no sabe si vino de una cámara o de un archivo.

```bash
python alpr_stream.py --save-crops          # sin -i: resuelve la fuente sola
python alpr_stream.py --list-cameras        # inventario de cámaras y salir
python alpr_stream.py --preview-camera 1    # vista previa de la cámara 1
python video_source.py --select -v          # diagnóstico independiente
```

### Android

Selecciona **automáticamente la cámara trasera**, sin intervención del usuario. Lee el inventario de Camera2 vía Termux:API y aplica esta cadena de fallback si no hay cámara trasera (tablets, dispositivos atípicos):

```
trasera → frontal → externa → cualquiera verificable → sondeo de índices 0-3
```

Los permisos en runtime (API 23+) se gestionan según el empaquetado, en este orden: `android.permissions` (python-for-android/Kivy), `ActivityCompat.requestPermissions` vía pyjnius/Chaquopy y, en Termux, el diálogo propio de Termux:API. Si el usuario deniega el permiso, el error indica la ruta exacta de Ajustes para concederlo.

En Termux:

```bash
pkg install termux-api python opencv-python
python alpr_stream.py --save-crops --no-video
```

Variables útiles: `ALPR_ANDROID_CAMERA_ID` fuerza un id concreto y `ALPR_FORCE_PLATFORM=android` permite probar la lógica desde un PC.

### PC (Windows / Linux / macOS)

Al iniciar sin `-i` aparece un menú con dos opciones:

```
  [1] Procesar video desde archivo
      (disco local, pendrive, disco externo, tarjeta SD)
  [2] Usar cámara en vivo
      (webcam integrada, cámara USB, cámara IP de la red)
```

- **Archivo** — explorador gráfico (tkinter) con la ubicación inicial en el primer medio extraíble detectado; si no hay entorno gráfico, un selector de texto que lista los medios montados y los videos que contienen. Antes de procesar valida existencia, permisos, tamaño, extensión y decodificación real del primer frame.
- **Cámara en vivo** — enumera los dispositivos con nombre, ID, resolución y fps, marcando cuáles entregaron imagen. Permite además introducir una URL RTSP/HTTP a mano y buscar cámaras IP en la red local.
- **Vista previa** — ventana con FPS reales superpuestos (`q`/`ESC` cierra). Sin entorno gráfico mide los FPS y guarda una captura JPEG para inspección.

Enumeración por sistema operativo:

| Plataforma | Método principal | Respaldos |
|---|---|---|
| Linux | `/sys/class/video4linux/*/name` + `v4l2-ctl --all` | sondeo de `/dev/video*`; avisa si falta el grupo `video` |
| Windows | DirectShow vía `pygrabber` | `Get-CimInstance Win32_PnPEntity`; sondeo de índices con `CAP_DSHOW` |
| macOS | `system_profiler SPCameraDataType` | sondeo AVFoundation |
| Cámaras IP | sondeo TCP de la subred local en los puertos 554, 8554, 8080, 80, 88 y 8000 | resolución DNS inversa y URL RTSP sugerida por fabricante |

El descubrimiento IP es un sondeo de puertos, no una verificación de stream: la URL propuesta debe completarse con la ruta y credenciales del fabricante. Solo explora la propia subred `/24` y rechaza rangos mayores de 1024 hosts.

### Manejo de errores

Cada fallo produce una excepción específica con instrucciones accionables: `CameraUnavailableError` (sin cámaras o sin frames), `CameraPermissionError` (permiso denegado, con la ruta de Ajustes), `VideoFileError` (inexistente, vacío, sin permisos o codec no soportado, sugiriendo el comando `ffmpeg` de conversión) y `StorageDisconnectedError`. Este último cubre el caso de retirar un pendrive **durante** el procesamiento: el script distingue el fin normal del video de la desaparición del medio y cierra conservando el CSV y el video generados hasta ese punto.

Todo el proceso de detección se registra con detalle (`-v` para nivel DEBUG) en el logger `alpr.source`: inventario previo, backend usado por cada dispositivo, motivo de descarte y resultado del sondeo, para depurar configuraciones de hardware distintas.

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

## Instalación en Android (Termux + Debian)

Para ejecutarlo en un teléfono sin root hay una guía dedicada:
**[docs/INSTALACION-ANDROID-TERMUX.md](docs/INSTALACION-ANDROID-TERMUX.md)** — Termux, `proot-distro`
con Debian, dependencias aarch64, fuentes de video válidas en Android, panel web, arranque
automático con Termux:Boot y expectativas de rendimiento.

## Instalación en Windows

Guía dedicada para PC con Windows 10/11:
**[docs/INSTALACION-WINDOWS.md](docs/INSTALACION-WINDOWS.md)** — Python y dependencias, permisos de
cámara, menú de selección de fuente, panel web, ejecución permanente con NSSM o tarea programada y
mantenimiento en PowerShell.

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
| `--select-source` | `ALPR_SELECT_SOURCE` | `false` | Forzar el menú de selección |
| `--list-cameras` | — | — | Enumerar cámaras y salir |
| `--preview-camera N` | — | — | Vista previa de la cámara N |
| `--scan-ip-cameras` | `ALPR_SCAN_IP_CAMERAS` | `false` | Buscar cámaras IP en la LAN |
| `--ip-subnet CIDR` | — | subred local | Subred a explorar (repetible) |

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

## Panel web

Interfaz de administración y monitorización servida por `web/web_api.py` (FastAPI + uvicorn). Lee el mismo CSV y los mismos recortes que genera `alpr_stream.py` y controla la ejecución de cada cámara con dos backends intercambiables:

| Backend | Cuándo se usa | Cómo ejecuta las cámaras |
|---|---|---|
| `systemd` | Linux con `systemctl` disponible | Instancias de la unidad `alpr-stream@<id>.service` |
| `proceso` | Windows, macOS, Termux o Linux sin systemd | Supervisor propio (`web/supervisor.py`): un proceso `alpr_stream.py` por cámara, reinicio automático con espera progresiva, archivos PID para reengancharse tras reiniciar el panel y cierre del árbol de procesos al detener |

`ALPR_WEB_BACKEND=auto` (por defecto) elige solo; `proceso` o `systemd` lo fuerzan. Sin control habilitado las acciones se simulan.

**Vistas**

- **Panel** — cámaras activas, detecciones del día, total registrado, confianza media, última detección y uso de disco; detecciones por hora apiladas por cámara (12/24/48 h), reparto por franja de confianza, placas más frecuentes y tira de últimas capturas. Se actualiza en vivo por SSE.
- **Cámaras** — alta, edición y borrado; iniciar/detener/reiniciar cada cámara con PID y tiempo en marcha; ver y **guardar en disco** su archivo `.env`; visor del registro en vivo (`datos/logs/<id>.log`); detección de hardware local (webcams y sondeo RTSP en la red).
- **Detecciones** — historial con filtros por placa, cámara, confianza mínima y fecha, paginación, visor de recortes y exportación a CSV.
- **Sistema** — backend activo, rutas efectivas (datos, configuración, registros, PID), intérprete y motor, política de reinicio automático, permisos y comandos de puesta en marcha.

**Puesta en marcha**

```bash
pip install fastapi uvicorn

# Con los datos reales del servicio
sudo -u alpr ALPR_WEB_DATA=/var/lib/alpr \
  python web/web_api.py --host 127.0.0.1 --port 8080

# Con datos sintéticos para evaluar la interfaz
python web/generar_demo.py --horas 48 --detecciones 420
python web/web_api.py --demo
```

En Windows todo se administra desde el panel, sin systemd:

```powershell
cd C:\alpr
powershell -ExecutionPolicy Bypass -File .\windows\preparar-entorno.ps1
powershell -ExecutionPolicy Bypass -File .\windows\iniciar-panel.ps1
```

Los scripts y plantillas están en [`windows/`](windows/README.md) (`panel.env.example`, tarea programada, servicio NSSM, firewall y purga de datos).

Para permitir iniciar, detener y escribir configuración desde el panel: `ALPR_WEB_ALLOW_CONTROL=true` (con backend `systemd` requiere permisos de `systemctl` para el usuario del panel).

**Variables de entorno**

| Variable | Por defecto | Descripción |
|---|---|---|
| `ALPR_WEB_DATA` | `/var/lib/alpr` | Directorio de datos (CSV, recortes, video) |
| `ALPR_WEB_CSV` | `<data>/placas.csv` | Ruta del CSV de detecciones |
| `ALPR_WEB_CAMERAS` | `<data>/camaras.json` | Almacén de cámaras del panel |
| `ALPR_WEB_UNIT` | `alpr-stream@` | Prefijo de la unidad systemd instanciada |
| `ALPR_WEB_BACKEND` | `auto` | `auto`, `systemd` o `proceso` |
| `ALPR_WEB_CONFIG` | `<data>/config` | Archivos `.env` por cámara |
| `ALPR_WEB_LOGS` | `<data>/logs` | Registro por cámara (`<id>.log`) |
| `ALPR_WEB_RUN` | `<data>/run` | Archivos PID para reengancharse a procesos vivos |
| `ALPR_WEB_PYTHON` | intérprete actual | Python con el que se lanza el motor |
| `ALPR_WEB_SCRIPT` | `alpr_stream.py` del repo | Motor ALPR que ejecuta el supervisor |
| `ALPR_WEB_BASE_ENV` | — | `.env` con valores comunes que hereda cada cámara |
| `ALPR_WEB_AUTORESTART` | `true` | Reinicia una cámara que muera (como `Restart=always`) |
| `ALPR_WEB_MAX_RESTARTS` | `0` | Límite de reinicios por cámara; `0` = sin límite |
| `ALPR_WEB_STOP_ON_EXIT` | `false` | Detener las cámaras al cerrar el panel |
| `ALPR_WEB_ALLOW_CONTROL` | `false` | Habilita iniciar/detener/reiniciar y guardar `.env` |
| `ALPR_WEB_HOST` / `ALPR_WEB_PORT` | `127.0.0.1` / `8080` | Escucha del servidor |
| `ALPR_WEB_DEMO` | `false` | Datos sintéticos en `web/demo-datos` |

**API** — `GET /api/estado`, `/api/camaras` (GET/POST), `/api/camaras/{id}` (PUT/DELETE), `/api/camaras/{id}/accion`, `/api/camaras/{id}/env` (GET/POST), `/api/camaras/{id}/registro`, `/api/camaras-detectadas`, `/api/detecciones`, `/api/detecciones.csv`, `/api/metricas`, `/api/imagen`, `/api/eventos` (SSE), `/api/salud`. Documentación interactiva en `/api/docs`.

> **Seguridad:** el panel no trae autenticación propia y muestra matrículas, que son dato personal en muchas jurisdicciones. Exponlo solo en la red interna o detrás de un proxy inverso con TLS y autenticación, y nunca directamente a Internet. Las credenciales de las URL RTSP se enmascaran en todas las respuestas de la API.

## Estructura del repositorio

| Archivo | Descripción |
|---|---|
| `alpr_stream.py` | Script principal: captura, detección, CSV, recortes y video anotado |
| `video_source.py` | Detección y selección de fuente multiplataforma (Android / PC) |
| `alpr.env.example` | Plantilla de configuración (`EnvironmentFile` de systemd) |
| `alpr-stream@.service` | Unidad systemd instanciada con `Restart=always` |
| `web/supervisor.py` | Supervisor de procesos multiplataforma usado por el panel sin systemd |
| `windows/` | Plantilla `panel.env` y scripts PowerShell para administrarlo todo desde la web |
| `install-alpr.sh` | Instalador: usuario, venv, directorios y permisos |
| `docs/INSTALACION-ANDROID-TERMUX.md` | Guía de instalación en Android con Termux y Debian (proot) |
| `docs/INSTALACION-WINDOWS.md` | Guía de instalación en Windows 10/11 (NSSM, tarea programada) |
| `web/web_api.py` | Backend del panel web (FastAPI): estado, cámaras, detecciones, SSE |
| `web/generar_demo.py` | Generador de datos sintéticos para probar el panel |
| `web/static/index.html` | Interfaz del panel (SPA con rutas por hash) |
| `web/static/styles.css` | Estilos del panel (tema de sala de control) |
| `web/static/app.js` | Lógica del panel: fetch, gráficas, diálogos y eventos en vivo |

## Notas operativas

- Si el MP4 de salida queda vacío, tu build de OpenCV no trae el codec: usa `--fourcc XVID` con extensión `.avi`.
- En CPU modesta, `--target-fps 5` suele bastar para control de accesos y evita saturar el equipo.
- Para grabación permanente conviene una política de retención (por ejemplo un `systemd-tmpfiles` o un timer que borre recortes y segmentos con más de N días).
- En el servicio systemd la fuente se fija siempre con `ALPR_INPUT`: sin consola, el menú interactivo se omite y el arranque falla con un mensaje explícito en vez de quedar bloqueado.
- En Linux, para acceder a `/dev/video*` añade tu usuario al grupo `video` (`sudo usermod -aG video $USER`).
- En Windows, `pip install pygrabber` mejora el nombrado de las cámaras (usa DirectShow, el mismo orden que OpenCV).
- Ajusta `--min-confidence` según la cámara: ángulos forzados y noche degradan el OCR.
- El tratamiento de matrículas puede constituir dato personal según la jurisdicción (RGPD y equivalentes). Define retención, base legal y control de acceso antes de desplegar en producción.

## Licencia

MIT — ver [LICENSE](LICENSE).
