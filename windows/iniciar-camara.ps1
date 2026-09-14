<#
.SYNOPSIS
    Ejecuta una cámara del ALPR en primer plano, sin pasar por el panel.
.DESCRIPTION
    Útil para diagnosticar: muestra la salida del motor en la consola. Combina
    config\alpr.env (valores comunes) con config\<id>.env (cámara concreta).
.EXAMPLE
    .\iniciar-camara.ps1 -Camara acceso-norte
.EXAMPLE
    .\iniciar-camara.ps1 -Fuente "rtsp://usuario:clave@192.168.1.40:554/Streaming/Channels/101"
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [string]$Camara = '',
    [string]$Fuente = '',
    [double]$Fps = 0,
    [double]$Confianza = 0
)

. "$PSScriptRoot\comun.ps1"
if (-not $Raiz) { $Raiz = Get-AlprHome }

Import-EnvFile (Join-Path $Raiz 'config\alpr.env') | Out-Null
if ($Camara) {
    $propio = Join-Path $Raiz "config\$Camara.env"
    Import-EnvFile $propio | Out-Null
    $env:ALPR_CAMERA_ID = $Camara
}
if ($Fuente)     { $env:ALPR_INPUT = $Fuente }
if ($Fps)        { $env:ALPR_TARGET_FPS = "$Fps" }
if ($Confianza)  { $env:ALPR_MIN_CONFIDENCE = "$Confianza" }
if (-not $env:ALPR_OUTPUT_DIR) { $env:ALPR_OUTPUT_DIR = Join-Path $Raiz 'datos' }
$env:PYTHONIOENCODING = 'utf-8'

$py = Get-AlprPython -Raiz $Raiz
Write-Paso "Motor ALPR · fuente: $($env:ALPR_INPUT)"
Write-Aviso 'Ctrl-C para detener. El CSV se escribe con flush por fila.'
& $py (Join-Path $Raiz 'alpr_stream.py') -v
