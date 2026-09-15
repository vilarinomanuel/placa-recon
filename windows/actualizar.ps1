<#
.SYNOPSIS
    Actualiza una instalación de placa-recon en Windows (por defecto C:\alpr).
.DESCRIPTION
    Detiene el panel, respalda los archivos que se van a sustituir, descarga la
    última versión del repositorio (git pull si es un clon, o el ZIP de GitHub si
    no lo es), conserva la configuración y los datos, añade a config\panel.env las
    claves nuevas que falten, crea datos\live para la vista en vivo y vuelve a
    arrancar el panel.

    Nunca toca: config\*.env, datos\ ni venv\.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\alpr\windows\actualizar.ps1
.EXAMPLE
    .\actualizar.ps1 -SinArrancar          # actualiza y deja el panel parado
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [string]$Rama = 'main',
    [string]$Repo = 'https://github.com/vilarinomanuel/placa-recon',
    [switch]$SinArrancar,
    [switch]$SinRespaldo
)

. "$PSScriptRoot\comun.ps1"
if (-not $Raiz) { $Raiz = Get-AlprHome }
if (-not (Test-Path (Join-Path $Raiz 'alpr_stream.py'))) {
    Write-Fallo "No encuentro alpr_stream.py en $Raiz. Indica la ruta con -Raiz C:\alpr"
    exit 1
}
Write-Paso "Actualizando la instalación en $Raiz"

# --- 1. Detener el panel ----------------------------------------------------
$reanudar = $false
$tarea = Get-ScheduledTask -TaskName 'ALPR Panel' -ErrorAction SilentlyContinue
if ($tarea -and $tarea.State -eq 'Running') {
    Stop-ScheduledTask -TaskName 'ALPR Panel'
    Write-Bien 'Tarea programada "ALPR Panel" detenida'
    $reanudar = $true
}
$servicio = Get-Service -Name 'ALPRPanel' -ErrorAction SilentlyContinue
if ($servicio -and $servicio.Status -eq 'Running') {
    Stop-Service -Name 'ALPRPanel'
    Write-Bien 'Servicio ALPRPanel detenido'
    $reanudar = $true
}
$sueltos = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'web_api\.py' }
foreach ($p in $sueltos) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Bien "Panel suelto detenido (PID $($p.ProcessId))"
}
# Las cámaras siguen ejecutándose: el panel se reengancha luego con datos\run.

# --- 2. Respaldo ------------------------------------------------------------
if (-not $SinRespaldo) {
    $sello = Get-Date -Format 'yyyyMMdd-HHmmss'
    $destino = Join-Path $Raiz "respaldo\codigo-$sello"
    New-Item -ItemType Directory -Force -Path $destino | Out-Null
    foreach ($item in @('alpr_stream.py', 'video_source.py', 'web', 'windows', 'docs', 'README.md')) {
        $origen = Join-Path $Raiz $item
        if (Test-Path $origen) { Copy-Item $origen $destino -Recurse -Force }
    }
    Write-Bien "Respaldo del código en $destino"
}

# --- 3. Traer la última versión --------------------------------------------
$esClon = Test-Path (Join-Path $Raiz '.git')
$git = Get-Command git -ErrorAction SilentlyContinue

if ($esClon -and $git) {
    Push-Location $Raiz
    try {
        & git fetch origin $Rama
        & git checkout $Rama
        & git pull --ff-only origin $Rama
        if ($LASTEXITCODE -ne 0) {
            Write-Aviso 'git pull no pudo avanzar (hay cambios locales).'
            Write-Aviso 'Revisa "git status" o vuelve a ejecutar con -SinRespaldo tras guardar tus cambios.'
            Pop-Location
            exit 1
        }
        Write-Bien "Repositorio actualizado a $(& git rev-parse --short HEAD)"
    } finally { if ((Get-Location).Path -eq $Raiz) { Pop-Location } }
} else {
    Write-Paso 'La carpeta no es un clon de git: se descarga el ZIP del repositorio'
    $tmp = Join-Path $env:TEMP "placa-recon-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'repo.zip'
    Invoke-WebRequest -Uri "$Repo/archive/refs/heads/$Rama.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $raizZip = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like 'placa-recon-*' } | Select-Object -First 1
    if (-not $raizZip) { Write-Fallo 'El ZIP descargado no tiene la estructura esperada'; exit 1 }
    # Se copia solo el código: config\ y datos\ quedan intactos.
    foreach ($item in @('alpr_stream.py', 'video_source.py', 'install-alpr.sh',
                        'alpr-stream@.service', 'alpr.env.example', 'README.md', 'LICENSE',
                        'web', 'windows', 'docs')) {
        $origen = Join-Path $raizZip.FullName $item
        if (Test-Path $origen) { Copy-Item $origen $Raiz -Recurse -Force }
    }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Bien "Código sustituido desde $Repo ($Rama)"
}

# --- 4. Dependencias -------------------------------------------------------
$py = Get-AlprPython -Raiz $Raiz
& $py -m pip install --quiet --upgrade fastapi uvicorn
Write-Bien 'fastapi y uvicorn al día'

# --- 5. Claves nuevas en panel.env y carpeta de la vista en vivo -----------
$panelEnv = Join-Path $Raiz 'config\panel.env'
$datos = Join-Path $Raiz 'datos'
if (Test-Path $panelEnv) { Import-EnvFile $panelEnv | Out-Null; if ($env:ALPR_WEB_DATA) { $datos = $env:ALPR_WEB_DATA } }
$live = if ($env:ALPR_WEB_LIVE) { $env:ALPR_WEB_LIVE } else { Join-Path $datos 'live' }
New-Item -ItemType Directory -Force -Path $live | Out-Null
Write-Bien "Directorio de vista en vivo: $live"

if (Test-Path $panelEnv) {
    $nuevas = [ordered]@{
        'ALPR_WEB_LIVE'         = $live
        'ALPR_WEB_LIVE_FPS'     = '3'
        'ALPR_WEB_LIVE_WIDTH'   = '640'
        'ALPR_WEB_LIVE_QUALITY' = '70'
        'ALPR_WEB_LIVE_MAX_AGE' = '12'
    }
    $texto = Get-Content -LiteralPath $panelEnv -Encoding UTF8
    $añadir = @()
    foreach ($clave in $nuevas.Keys) {
        if (-not ($texto -match "^\s*$clave\s*=")) { $añadir += "$clave=$($nuevas[$clave])" }
    }
    if ($añadir.Count) {
        Add-Content -LiteralPath $panelEnv -Encoding UTF8 -Value (@('', '# Vista en vivo del panel (añadido por actualizar.ps1)') + $añadir)
        Write-Bien "$($añadir.Count) variables nuevas añadidas a $panelEnv"
    } else {
        Write-Bien 'panel.env ya tenía las variables de la vista en vivo'
    }
} else {
    Write-Aviso "No existe $panelEnv; cópialo de windows\panel.env.example"
}

# --- 6. Arrancar de nuevo --------------------------------------------------
if ($SinArrancar) {
    Write-Paso 'Actualización terminada. El panel queda detenido (-SinArrancar).'
    exit 0
}
if ($servicio) {
    Start-Service -Name 'ALPRPanel'
    Write-Bien 'Servicio ALPRPanel arrancado'
} elseif ($tarea) {
    Start-ScheduledTask -TaskName 'ALPR Panel'
    Write-Bien 'Tarea programada "ALPR Panel" arrancada'
} elseif ($reanudar -or $sueltos) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $Raiz 'windows\iniciar-panel.ps1')
} else {
    Write-Paso 'Arranca el panel con: powershell -ExecutionPolicy Bypass -File .\windows\iniciar-panel.ps1'
}

Write-Paso 'Reinicia cada cámara desde el panel para que empiece a publicar la vista en vivo.'
