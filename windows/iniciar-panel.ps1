<#
.SYNOPSIS
    Arranca el panel web de placa-recon en este PC.
.DESCRIPTION
    Carga config\panel.env, comprueba el puerto, lanza uvicorn con el venv del
    proyecto y abre el navegador. Con -Servicio omite el navegador (uso en NSSM).
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\alpr\windows\iniciar-panel.ps1
.EXAMPLE
    .\iniciar-panel.ps1 -Puerto 8090 -Escucha 0.0.0.0
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [string]$Escucha = '',
    [int]$Puerto = 0,
    [switch]$Servicio,
    [switch]$Detallado
)

. "$PSScriptRoot\comun.ps1"
if (-not $Raiz) { $Raiz = Get-AlprHome }

$R = Set-AlprEntorno -Raiz $Raiz
Import-EnvFile $R.PanelEnv | Out-Null
if ($Escucha) { $env:ALPR_WEB_HOST = $Escucha }
if ($Puerto)  { $env:ALPR_WEB_PORT = "$Puerto" }
if (-not $env:ALPR_WEB_HOST) { $env:ALPR_WEB_HOST = '127.0.0.1' }
if (-not $env:ALPR_WEB_PORT) { $env:ALPR_WEB_PORT = '8080' }
if (-not $env:ALPR_WEB_BACKEND) { $env:ALPR_WEB_BACKEND = 'proceso' }
if (-not $env:ALPR_WEB_SCRIPT)  { $env:ALPR_WEB_SCRIPT  = Join-Path $Raiz 'alpr_stream.py' }
$py = Get-AlprPython -Raiz $Raiz
$env:ALPR_WEB_PYTHON = $py
$env:PYTHONIOENCODING = 'utf-8'

$enUso = Get-NetTCPConnection -State Listen -LocalPort ([int]$env:ALPR_WEB_PORT) -ErrorAction SilentlyContinue
if ($enUso) {
    Write-Fallo "El puerto $($env:ALPR_WEB_PORT) ya está ocupado por el PID $($enUso[0].OwningProcess)."
    Write-Aviso 'Cierra ese proceso o arranca el panel en otro puerto: -Puerto 8090'
    exit 1
}

Write-Paso "Panel en http://$($env:ALPR_WEB_HOST):$($env:ALPR_WEB_PORT)  (backend: $($env:ALPR_WEB_BACKEND))"
Write-Host "  Datos: $env:ALPR_WEB_DATA"
if ($env:ALPR_WEB_HOST -eq '0.0.0.0') {
    Write-Aviso 'Escuchando en toda la red: úsalo solo en red interna de confianza. El panel muestra matrículas.'
}

if (-not $Servicio) {
    $destino = "http://127.0.0.1:$($env:ALPR_WEB_PORT)/"
    Start-Job -ScriptBlock { param($u) Start-Sleep 3; Start-Process $u } -ArgumentList $destino | Out-Null
}

$argumentos = @((Join-Path $Raiz 'web\web_api.py'), '--host', $env:ALPR_WEB_HOST, '--port', $env:ALPR_WEB_PORT)
if ($Detallado) { $argumentos += '-v' }
& $py @argumentos
