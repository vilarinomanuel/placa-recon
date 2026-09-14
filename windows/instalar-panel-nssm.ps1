<#
.SYNOPSIS
    Instala el panel como servicio de Windows con NSSM (arranque sin sesión).
.DESCRIPTION
    Alternativa a la tarea programada para equipos que trabajan solo con cámaras
    RTSP. El servicio corre como SYSTEM y no tiene acceso a cámaras USB.
    Requiere nssm.exe (https://nssm.cc/download) y consola de Administrador.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\alpr\windows\instalar-panel-nssm.ps1 -Nssm C:\nssm\nssm.exe
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [string]$Nssm = 'nssm.exe',
    [string]$Servicio = 'ALPRPanel',
    [switch]$Desinstalar
)

. "$PSScriptRoot\comun.ps1"
Assert-Administrador
if (-not $Raiz) { $Raiz = Get-AlprHome }

if (-not (Get-Command $Nssm -ErrorAction SilentlyContinue)) {
    throw "No encuentro nssm.exe. Descárgalo de https://nssm.cc/download y pasa la ruta con -Nssm."
}

if ($Desinstalar) {
    & $Nssm stop $Servicio 2>$null | Out-Null
    & $Nssm remove $Servicio confirm
    Write-Bien "Servicio $Servicio eliminado"
    exit 0
}

$script = Join-Path $PSScriptRoot 'iniciar-panel.ps1'
$logs   = Join-Path $Raiz 'datos\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

& $Nssm install $Servicio 'powershell.exe' `
    "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Servicio"
& $Nssm set $Servicio AppDirectory   $Raiz
& $Nssm set $Servicio DisplayName    'placa-recon · panel ALPR'
& $Nssm set $Servicio Description    'Panel web de administración y supervisión del ALPR'
& $Nssm set $Servicio Start          SERVICE_AUTO_START
& $Nssm set $Servicio AppStdout      (Join-Path $logs 'panel.out.log')
& $Nssm set $Servicio AppStderr      (Join-Path $logs 'panel.err.log')
& $Nssm set $Servicio AppRotateFiles 1
& $Nssm set $Servicio AppRotateBytes 10485760
& $Nssm set $Servicio AppExit Default Restart
& $Nssm set $Servicio AppRestartDelay 5000
& $Nssm start $Servicio

Write-Bien "Servicio $Servicio instalado y arrancado"
Write-Aviso 'Como SYSTEM no hay acceso a cámaras USB: usa RTSP, o instalar-tarea-panel.ps1.'
