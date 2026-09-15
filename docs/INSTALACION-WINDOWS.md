# Instalación en Windows

Guía paso a paso para ejecutar `placa-recon` en un PC con Windows 10 u 11 (x64), tanto de forma
interactiva como en segundo plano de manera permanente.

Todo se hace desde **PowerShell**. Donde haga falta consola de administrador se indica de forma
explícita.

---

## 1. Requisitos previos

| Componente | Versión | Notas |
|---|---|---|
| Windows | 10 21H2 / 11 x64 | también funciona en Windows Server 2019+ |
| Python | 3.11, 3.12 o 3.13 | `onnxruntime` publica ruedas para 3.11+ ([PyPI](https://pypi.org/project/onnxruntime/)) |
| Git | cualquiera reciente | opcional si descargas el ZIP |
| Disco | ~2 GB | modelos ONNX, recortes y video anotado |

Instala Python y Git con `winget` (consola normal):

```powershell
winget install --id Python.Python.3.12 --source winget
winget install --id Git.Git --source winget
```

Cierra y vuelve a abrir PowerShell para que el `PATH` se actualice, y comprueba:

```powershell
python --version
git --version
```

Si `python` abre la Microsoft Store, desactiva los alias en
Configuración → Aplicaciones → Configuración avanzada de aplicaciones → Alias de ejecución de aplicaciones.

## 2. Clonar el proyecto

```powershell
New-Item -ItemType Directory -Force C:\alpr | Out-Null
cd C:\alpr
git clone https://github.com/vilarinomanuel/placa-recon.git .
```

Sin Git: descarga el ZIP desde GitHub y descomprímelo en `C:\alpr`.

## 3. Entorno virtual y dependencias

```powershell
cd C:\alpr
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip wheel
```

Si PowerShell bloquea el script de activación:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

Instala las dependencias. `fast-alpr` **no incluye ningún runtime ONNX por defecto**, hay que pedir
el extra ([PyPI de fast-alpr](https://pypi.org/project/fast-alpr/)):

```powershell
pip install "fast-alpr[onnx]" opencv-python
pip install pygrabber          # nombres reales de las cámaras vía DirectShow
pip install fastapi uvicorn    # solo si vas a usar el panel web
```

- Usa `opencv-python` (no la variante `headless`) si quieres la ventana de previsualización de
  cámaras; en un servidor sin escritorio instala `opencv-python-headless`.
- `pygrabber` enumera los dispositivos DirectShow con el mismo orden que OpenCV
  ([PyPI](https://pypi.org/project/pygrabber/)), así el menú muestra "Logitech C920" en vez de
  "Cámara 0".

Con GPU NVIDIA y CUDA ya instalado puedes sustituir el backend por el acelerado:

```powershell
pip uninstall -y onnxruntime
pip install "fast-alpr[onnx-gpu]"
```

Verificación:

```powershell
python -c "import cv2, onnxruntime; print(cv2.__version__, onnxruntime.get_available_providers())"
```

La primera ejecución descarga los modelos ONNX (unos 100 MB) a `%USERPROFILE%\.cache`.

## 4. Permisos de cámara

Windows exige permiso explícito incluso para apps de escritorio:
Configuración → Privacidad y seguridad → Cámara → activa **Permitir que las aplicaciones de
escritorio accedan a la cámara**. Sin esto, OpenCV abre el dispositivo y devuelve fotogramas negros
o falla silenciosamente.

## 5. Primera ejecución interactiva

```powershell
cd C:\alpr
.\venv\Scripts\Activate.ps1
python alpr_stream.py -v
```

Al arrancar sin `ALPR_INPUT` aparece el menú de selección de fuente:

```
1) Procesar un archivo de video
2) Usar una cámara en vivo
```

- **Archivo**: acepta ruta pegada, con o sin comillas, y arrastrar-y-soltar del Explorador.
- **Cámara en vivo**: enumera las cámaras detectadas con nombre, resolución e índice, y permite
  previsualizar antes de confirmar. `q` o `Esc` cierran la previsualización.

Ejemplos directos sin menú:

```powershell
# Archivo
python alpr_stream.py --input "D:\videos\acceso.mp4" --output-dir C:\alpr\datos

# Webcam USB por índice
python alpr_stream.py --input 0 --target-fps 8

# Cámara IP RTSP
python alpr_stream.py --input "rtsp://operador:clave@192.168.1.40:554/Streaming/Channels/101"
```

## 6. Configuración por entorno

Copia la plantilla y edítala:

```powershell
Copy-Item alpr.env.example alpr.env
notepad alpr.env
```

En Windows `alpr.env` no se carga solo (eso lo hace systemd en Linux). Crea un lanzador
`C:\alpr\iniciar.ps1` que lea el archivo y arranque el motor:

```powershell
# C:\alpr\iniciar.ps1
Set-Location C:\alpr
Get-Content .\alpr.env | Where-Object { $_ -match '^\s*[^#\s]+=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
}
& .\venv\Scripts\python.exe .\alpr_stream.py
```

Ejecútalo con `powershell -ExecutionPolicy Bypass -File C:\alpr\iniciar.ps1`.

Valores típicos en `alpr.env` para un PC de escritorio:

```ini
ALPR_INPUT=rtsp://operador:clave@192.168.1.40:554/Streaming/Channels/101
ALPR_OUTPUT_DIR=C:\alpr\datos
ALPR_CSV=C:\alpr\datos\placas.csv
ALPR_MIN_CONFIDENCE=0.80
ALPR_TARGET_FPS=8
ALPR_DEDUPE_SECONDS=5
ALPR_SAVE_CROPS=true
ALPR_SAVE_VIDEO=true
ALPR_FOURCC=XVID
```

Protege el archivo, porque contiene la contraseña de la cámara:

```powershell
icacls C:\alpr\alpr.env /inheritance:r /grant:r "$env:USERNAME:(R)" /grant:r "SYSTEM:(R)"
```

## 7. Panel web: administrarlo todo desde el navegador

Desde esta versión el panel puede **ejecutar y vigilar las cámaras en Windows**, sin systemd: lanza un
proceso `alpr_stream.py` por cámara, lo reinicia si muere y guarda su registro. Los scripts de apoyo
están en `C:\alpr\windows`.

### 7.1 Preparar el entorno una sola vez

```powershell
cd C:\alpr
powershell -ExecutionPolicy Bypass -File .\windows\preparar-entorno.ps1
```

Crea `config\`, `datos\{crops,video,logs,run}` y `respaldo\`, copia `windows\panel.env.example` a
`config\panel.env`, restringe sus permisos con `icacls`, inicializa `datos\camaras.json` y comprueba
que el venv tenga `cv2`, `onnxruntime`, `fast_alpr`, `fastapi` y `uvicorn`. Si falta algo:

```powershell
.\windows\preparar-entorno.ps1 -InstalarDependencias
```

### 7.2 Ajustar `config\panel.env`

Lo importante ya viene resuelto para `C:\alpr`:

```ini
ALPR_WEB_DATA=C:\alpr\datos
ALPR_WEB_CONFIG=C:\alpr\config
ALPR_WEB_LOGS=C:\alpr\datos\logs
ALPR_WEB_RUN=C:\alpr\datos\run
ALPR_WEB_SCRIPT=C:\alpr\alpr_stream.py
ALPR_WEB_PYTHON=C:\alpr\venv\Scripts\python.exe
ALPR_WEB_BACKEND=proceso
ALPR_WEB_ALLOW_CONTROL=true
ALPR_WEB_AUTORESTART=true
ALPR_WEB_HOST=127.0.0.1
ALPR_WEB_PORT=8080
```

`ALPR_WEB_ALLOW_CONTROL=true` es lo que habilita los botones de iniciar, detener, reiniciar y guardar
la configuración de cada cámara. `ALPR_WEB_BACKEND=proceso` fuerza el supervisor propio; con `auto` se
elegiría igual en Windows, porque no hay `systemctl`.

### 7.3 Arrancar el panel

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\iniciar-panel.ps1
```

Carga `config\panel.env`, avisa si el puerto está ocupado, abre `http://127.0.0.1:8080/` en el
navegador y deja el servidor en la consola (Ctrl-C para cerrarlo). Opciones útiles:

```powershell
.\windows\iniciar-panel.ps1 -Puerto 8090          # otro puerto
.\windows\iniciar-panel.ps1 -Escucha 0.0.0.0      # visible en la red interna
.\windows\iniciar-panel.ps1 -Detallado            # registro DEBUG
```

### 7.4 Flujo de trabajo en el panel

1. **Cámaras → Detectar hardware**: lista las webcams locales y, marcando la casilla, sondea RTSP en la
   red. «Usar» rellena el formulario de alta.
2. Para una IP concreta, **Añadir cámara** con la URL completa
   (`rtsp://usuario:clave@192.168.1.40:554/Streaming/Channels/101`), confianza mínima y FPS objetivo.
3. Botón del engranaje → **Guardar en disco**: escribe `config\<id>.env` con la configuración de esa
   cámara. Las credenciales se muestran enmascaradas, pero se guardan completas en el servidor.
4. **Iniciar**: el panel lanza el proceso y la tarjeta pasa a «En ejecución» con su PID y tiempo en
   marcha. Si el motor falla al arrancar, el aviso incluye las últimas líneas del registro.
5. Botón del ojo → **Vista en vivo**: la tarjeta muestra una miniatura de lo que ve la cámara y, al
   pulsarla, se abre una ventana ampliada. El motor publica el último fotograma anotado en
   `datos\live\<id>.jpg` (3 fps, 640 px por defecto); ajústalo con `ALPR_WEB_LIVE_FPS`,
   `ALPR_WEB_LIVE_WIDTH` y `ALPR_WEB_LIVE_QUALITY` en `config\panel.env`. El indicador pasa a
   «Congelada» si no llega ningún fotograma nuevo en 12 s (`ALPR_WEB_LIVE_MAX_AGE`) y a «Sin señal»
   con la cámara detenida.
6. Botón del documento → **Registro**: muestra `datos\logs\<id>.log` y se refresca cada 4 s.
7. **Panel** y **Detecciones** leen `datos\placas.csv` y los recortes en vivo por SSE.

Si el panel se cierra, los procesos siguen y al volver a abrirlo se reengancha a ellos usando
`datos\run\<id>.pid.json`. Para que se detengan al salir, pon `ALPR_WEB_STOP_ON_EXIT=true`.

### 7.5 Abrir el puerto a la red interna

```powershell
# Consola de administrador
.\windows\abrir-firewall.ps1 -Puerto 8080 -Origen 192.168.1.0/24
.\windows\iniciar-panel.ps1 -Escucha 0.0.0.0
```

El panel no trae autenticación propia y muestra matrículas: mantenlo en red privada o detrás de un
proxy inverso con TLS y autenticación. Nunca lo publiques en Internet.

### 7.6 Diagnóstico y modo demostración

```powershell
.\windows\iniciar-camara.ps1 -Camara acceso-norte     # motor en primer plano, sin panel
python web\generar_demo.py --horas 48 --detecciones 420
python web\web_api.py --demo                          # interfaz con datos sintéticos
```

## 8. Ejecución permanente

### Opción A — Tarea programada al iniciar sesión (recomendada)

Es la única vía que permite usar **cámaras USB**, porque corre en tu sesión de usuario:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\instalar-tarea-panel.ps1
Start-ScheduledTask -TaskName "ALPR Panel"
```

La tarea se reinicia sola (999 intentos, 1 min) y no tiene límite de duración. Con
`ALPR_WEB_AUTORESTART=true` el panel, a su vez, revive cada cámara que muera: el equivalente a
`Restart=always` de systemd. Para quitarla: `.\windows\instalar-tarea-panel.ps1 -Desinstalar`.

Desactiva la suspensión si debe funcionar 24/7:

```powershell
powercfg /change standby-timeout-ac 0
```

### Opción B — Servicio de Windows con NSSM (solo RTSP)

Arranca sin que nadie inicie sesión, pero corre como `SYSTEM` y **no ve cámaras USB**
([referencia habitual](https://stackoverflow.com/questions/32404/how-do-you-run-a-python-script-as-a-service-in-windows)).

```powershell
winget install --id NSSM.NSSM --source winget
# Consola de administrador
.\windows\instalar-panel-nssm.ps1 -Nssm C:\nssm\nssm.exe
```

Registra `ALPRPanel` con arranque automático, reinicio a los 5 s y rotación de
`datos\logs\panel.out.log`. Gestión: `nssm restart ALPRPanel`, `nssm stop ALPRPanel`,
`.\windows\instalar-panel-nssm.ps1 -Desinstalar`.

Las cámaras las sigue lanzando el panel, así que no hace falta un servicio por cámara.

### Opción C — Un servicio por cámara, sin panel

Si prefieres que el ALPR no dependa del panel, instala cada cámara como servicio propio:

```powershell
nssm install ALPRNorte "C:\alpr\venv\Scripts\python.exe" "C:\alpr\alpr_stream.py"
nssm set ALPRNorte AppDirectory C:\alpr
nssm set ALPRNorte AppStdout C:\alpr\datos\logs\acceso-norte.log
nssm set ALPRNorte AppRotateFiles 1
nssm set ALPRNorte Start SERVICE_AUTO_START
nssm set ALPRNorte AppEnvironmentExtra ALPR_CAMERA_ID=acceso-norte ALPR_INPUT=rtsp://operador:clave@192.168.1.40:554/Streaming/Channels/101 ALPR_OUTPUT_DIR=C:\alpr\datos ALPR_TARGET_FPS=8
nssm start ALPRNorte
```

En ese caso deja `ALPR_WEB_ALLOW_CONTROL=false` para que el panel quede en modo solo lectura y no
compita con los servicios por la misma cámara.

## 9. Mantenimiento

```powershell
# Actualizar
cd C:\alpr; git pull; .\venv\Scripts\Activate.ps1; pip install -U "fast-alpr[onnx]"

# Retención de datos (borra recortes, video y logs de más de 30 días)
.\windows\purgar-datos.ps1 -Dias 30 -Simular
.\windows\purgar-datos.ps1 -Dias 30
.\windows\purgar-datos.ps1 -InstalarTarea -Dias 30   # tarea diaria 03:30, como administrador

# Espacio ocupado por recortes
"{0:N1} GB" -f ((Get-ChildItem C:\alpr\datos\crops -Recurse -File | Measure-Object Length -Sum).Sum / 1GB)

# Purgar recortes de más de 14 días
Get-ChildItem C:\alpr\datos\crops -Recurse -File |
  Where-Object LastWriteTime -lt (Get-Date).AddDays(-14) | Remove-Item -Force

# Copia del CSV
Copy-Item C:\alpr\datos\placas.csv "C:\alpr\respaldo\placas-$(Get-Date -f yyyy-MM-dd).csv"
```

Para automatizar la purga —el equivalente al timer de retención pendiente en Linux— registra el
bloque anterior como tarea programada diaria.

## Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `python` abre la Microsoft Store | Desactiva los alias de ejecución de aplicaciones (paso 1) |
| `Activate.ps1 no se puede cargar` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `No matching distribution found for onnxruntime` | Python anterior a 3.11 o de 32 bits: instala Python 3.12 x64 |
| Fotogramas negros desde la webcam | Falta el permiso de cámara para apps de escritorio (paso 4) |
| Las cámaras salen como "Cámara 0" | Instala `pygrabber` |
| El servicio no encuentra la webcam | Como `SYSTEM` no hay acceso a USB ni a la sesión: usa RTSP o la opción B |
| MP4 de salida vacío (0 KB) | El build de OpenCV no trae el codec: `--fourcc XVID` con extensión `.avi` |
| RTSP con retardo creciente | Baja `--target-fps`; el lector en hilo aparte ya descarta fotogramas atrasados |
| Rutas con espacios fallan | Entrecomilla siempre: `--input "D:\mis videos\acceso.mp4"` |
| Tildes ilegibles en consola | `chcp 65001` antes de ejecutar, o usa Windows Terminal |

## Aviso legal

Las matrículas son dato personal en muchas jurisdicciones (RGPD y equivalentes). Define retención,
base legal y control de acceso antes de desplegar en producción.
