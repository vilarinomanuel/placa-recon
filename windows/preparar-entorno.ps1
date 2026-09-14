<#
.SYNOPSIS
    Prepara C:\alpr para administrar todo el ALPR desde el panel web.
.DESCRIPTION
    Crea la estructura de directorios, copia las plantillas de configuración,
    restringe permisos de los archivos con credenciales y comprueba que el venv
    y las dependencias estén completos. Es idempotente: se puede repetir.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\alpr\windows\preparar-entorno.ps1
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [switch]$InstalarDependencias
)

. "$PSScriptRoot\comun.ps1"
if (-not $Raiz) { $Raiz = Get-AlprHome }

Write-Paso "Preparando la instalación en $Raiz"

foreach ($sub in 'datos', 'datos\crops', 'datos\video', 'datos\logs', 'datos\run', 'config', 'respaldo') {
    $ruta = Join-Path $Raiz $sub
    if (-not (Test-Path $ruta)) {
        New-Item -ItemType Directory -Force -Path $ruta | Out-Null
        Write-Bien "creado $ruta"
    }
}

Write-Paso 'Configuración del panel'
$panelEjemplo = Join-Path $PSScriptRoot 'panel.env.example'
$panelEnv     = Join-Path $Raiz 'config\panel.env'
if (-not (Test-Path $panelEnv)) {
    Copy-Item $panelEjemplo $panelEnv
    (Get-Content $panelEnv -Raw).Replace('C:\alpr', $Raiz) | Set-Content $panelEnv -Encoding UTF8
    Write-Bien "creado $panelEnv"
} else {
    Write-Aviso "$panelEnv ya existe: se conserva"
}
Protect-Archivo $panelEnv

Write-Paso 'Configuración base del motor'
$alprEjemplo = Join-Path $Raiz 'alpr.env.example'
$alprEnv     = Join-Path $Raiz 'config\alpr.env'
if ((Test-Path $alprEjemplo) -and -not (Test-Path $alprEnv)) {
    Copy-Item $alprEjemplo $alprEnv
    Write-Bien "creado $alprEnv (valores comunes a todas las cámaras)"
} elseif (Test-Path $alprEnv) {
    Write-Aviso "$alprEnv ya existe: se conserva"
}
Protect-Archivo $alprEnv

Write-Paso 'Almacén de cámaras'
$camaras = Join-Path $Raiz 'datos\camaras.json'
if (-not (Test-Path $camaras)) {
    '[]' | Set-Content $camaras -Encoding UTF8
    Write-Bien "creado $camaras (vacío: añade las cámaras desde el panel)"
} else {
    $n = (Get-Content $camaras -Raw | ConvertFrom-Json).Count
    Write-Bien "$camaras contiene $n cámara(s)"
}

Write-Paso 'Comprobando el entorno de Python'
try {
    $py = Get-AlprPython -Raiz $Raiz
    Write-Bien "intérprete $py"
} catch {
    Write-Fallo $_.Exception.Message
    Write-Aviso "Crea el venv:  cd $Raiz ; python -m venv venv"
    exit 1
}

if ($InstalarDependencias) {
    Write-Paso 'Instalando dependencias'
    & $py -m pip install --upgrade pip wheel
    & $py -m pip install "fast-alpr[onnx]" opencv-python pygrabber fastapi uvicorn
}

$faltan = @()
foreach ($mod in 'cv2', 'onnxruntime', 'fast_alpr', 'fastapi', 'uvicorn') {
    & $py -c "import $mod" 2>$null
    if ($LASTEXITCODE -ne 0) { $faltan += $mod }
}
if ($faltan.Count) {
    Write-Fallo ("Faltan módulos: " + ($faltan -join ', '))
    Write-Aviso "Instálalos con:  .\windows\preparar-entorno.ps1 -InstalarDependencias"
} else {
    Write-Bien 'Todas las dependencias están presentes'
}

Write-Paso 'Resumen'
Write-Host "  Raíz            : $Raiz"
Write-Host "  Datos           : $(Join-Path $Raiz 'datos')"
Write-Host "  Configuración   : $(Join-Path $Raiz 'config')"
Write-Host "  Registros       : $(Join-Path $Raiz 'datos\logs')"
Write-Host ''
Write-Host '  Siguiente paso: iniciar el panel' -ForegroundColor Cyan
Write-Host "    powershell -ExecutionPolicy Bypass -File $(Join-Path $PSScriptRoot 'iniciar-panel.ps1')"
