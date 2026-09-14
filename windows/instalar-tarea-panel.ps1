<#
.SYNOPSIS
    Registra el panel como tarea programada que arranca al iniciar sesión.
.DESCRIPTION
    Es la forma recomendada en Windows cuando se usan cámaras USB: la tarea corre
    con tu propia cuenta, así que el motor ALPR sí puede abrir la webcam (un
    servicio como SYSTEM no puede). Reinicia la tarea si el proceso muere.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\alpr\windows\instalar-tarea-panel.ps1
.EXAMPLE
    .\instalar-tarea-panel.ps1 -Desinstalar
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [string]$Nombre = 'ALPR Panel',
    [switch]$Desinstalar
)

. "$PSScriptRoot\comun.ps1"
if (-not $Raiz) { $Raiz = Get-AlprHome }

if ($Desinstalar) {
    Unregister-ScheduledTask -TaskName $Nombre -Confirm:$false -ErrorAction SilentlyContinue
    Write-Bien "Tarea «$Nombre» eliminada"
    exit 0
}

$script = Join-Path $PSScriptRoot 'iniciar-panel.ps1'
$accion = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Servicio" `
    -WorkingDirectory $Raiz
$disparador = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$ajustes = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $Nombre -Action $accion -Trigger $disparador `
    -Settings $ajustes -Description 'Panel web de placa-recon (ALPR)' -Force | Out-Null
Write-Bien "Tarea «$Nombre» registrada para $env:USERNAME"
Write-Host '  Arrancar ahora :  Start-ScheduledTask -TaskName "' -NoNewline; Write-Host "$Nombre`""
Write-Host '  Detener        :  Stop-ScheduledTask  -TaskName "' -NoNewline; Write-Host "$Nombre`""
Write-Aviso 'El panel queda oculto: consúltalo en http://127.0.0.1:8080/'
