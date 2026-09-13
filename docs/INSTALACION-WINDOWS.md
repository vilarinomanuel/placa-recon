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

## 7. Panel web

```powershell
cd C:\alpr
.\venv\Scripts\Activate.ps1
$env:ALPR_WEB_DATA = "C:\alpr\datos"
python web\web_api.py --host 127.0.0.1 --port 8080
```

Abre `http://127.0.0.1:8080`. Para verlo desde otros equipos de la red interna:

```powershell
# Consola de administrador
New-NetFirewallRule -DisplayName "ALPR panel 8080" -Direction Inbound `
  -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private
python web\web_api.py --host 0.0.0.0 --port 8080
```

El panel no trae autenticación propia y muestra matrículas: mantenlo en red privada o detrás de un
proxy inverso con TLS y autenticación. Nunca lo publiques en Internet.

Modo demostración, para evaluar la interfaz sin cámara:

```powershell
python web\generar_demo.py --horas 48 --detecciones 420
python web\web_api.py --demo
```

En Windows las acciones de iniciar/detener cámaras del panel se simulan: la gestión de unidades es
específica de systemd. Ahí conviene el paso 8.

## 8. Ejecución permanente

### Opción A — NSSM (recomendada, equivalente a `Restart=always`)

NSSM reinicia el proceso automáticamente si termina, que es justo lo que hace la unidad systemd del
proyecto ([referencia habitual](https://stackoverflow.com/questions/32404/how-do-you-run-a-python-script-as-a-service-in-windows)).

```powershell
winget install --id NSSM.NSSM --source winget
# Consola de administrador
nssm install ALPRStream "C:\alpr\venv\Scripts\python.exe" "C:\alpr\alpr_stream.py"
nssm set ALPRStream AppDirectory C:\alpr
nssm set ALPRStream AppStdout C:\alpr\datos\alpr.log
nssm set ALPRStream AppStderr C:\alpr\datos\alpr.log
nssm set ALPRStream AppRotateFiles 1
nssm set ALPRStream Start SERVICE_AUTO_START
nssm set ALPRStream AppEnvironmentExtra ALPR_INPUT=rtsp://operador:clave@192.168.1.40:554/Streaming/Channels/101 ALPR_OUTPUT_DIR=C:\alpr\datos ALPR_TARGET_FPS=8
nssm start ALPRStream
```

Servicio del panel web, aparte:

```powershell
nssm install ALPRPanel "C:\alpr\venv\Scripts\python.exe" "C:\alpr\web\web_api.py --host 127.0.0.1 --port 8080"
nssm set ALPRPanel AppDirectory C:\alpr
nssm set ALPRPanel AppEnvironmentExtra ALPR_WEB_DATA=C:\alpr\datos
nssm start ALPRPanel
```

Gestión: `nssm restart ALPRStream`, `nssm stop ALPRStream`, `nssm remove ALPRStream confirm`.

Un servicio corre como `SYSTEM` y **no tiene acceso a cámaras USB ni a la sesión de escritorio**:
para servicio usa siempre fuentes RTSP. Si necesitas la webcam local, usa la opción B.

### Opción B — Tarea programada al inicio de sesión

```powershell
$acc = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File C:\alpr\iniciar.ps1"
$trg = New-ScheduledTaskTrigger -AtLogOn
$set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "ALPR Stream" -Action $acc -Trigger $trg -Settings $set
```

Esta vía sí ve las cámaras USB, porque corre en tu sesión. Desactiva la suspensión del equipo
(`powercfg /change standby-timeout-ac 0`) si debe funcionar 24/7.

## 9. Mantenimiento

```powershell
# Actualizar
cd C:\alpr; git pull; .\venv\Scripts\Activate.ps1; pip install -U "fast-alpr[onnx]"

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
