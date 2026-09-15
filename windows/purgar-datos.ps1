<#
.SYNOPSIS
    Retención de datos: borra recortes, videos y registros antiguos.
.DESCRIPTION
    Equivalente en Windows al timer de limpieza de Linux. Registra el resultado
    en datos\logs\purga.log. Ejecútalo a mano o con una tarea programada diaria.
.EXAMPLE
    .\purgar-datos.ps1 -Dias 30
.EXAMPLE
    .\purgar-datos.ps1 -Dias 30 -Simular
.EXAMPLE
    .\purgar-datos.ps1 -InstalarTarea -Dias 30
#>
[CmdletBinding()]
param(
    [string]$Raiz = '',
    [int]$Dias = 30,
    [switch]$Simular,
    [switch]$InstalarTarea
)

. "$PSScriptRoot\comun.ps1"
if (-not $Raiz) { $Raiz = Get-AlprHome }

if ($InstalarTarea) {
    Assert-Administrador
    $accion = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Dias $Dias"
    $disparador = New-ScheduledTaskTrigger -Daily -At 03:30
    Register-ScheduledTask -TaskName 'ALPR purga de datos' -Action $accion `
        -Trigger $disparador -Description "Borra capturas de más de $Dias días" -Force | Out-Null
    Write-Bien "Tarea diaria registrada (03:30, retención $Dias días)"
    exit 0
}

$limite = (Get-Date).AddDays(-$Dias)
$R      = Get-AlprRutas -Raiz $Raiz
$datos  = $R.Datos
$total  = 0
$bytes  = 0

foreach ($sub in 'crops', 'video', 'logs') {
    $ruta = Join-Path $datos $sub
    if (-not (Test-Path $ruta)) { continue }
    $viejos = Get-ChildItem -LiteralPath $ruta -Recurse -File |
        Where-Object { $_.LastWriteTime -lt $limite -and $_.Name -ne 'purga.log' }
    foreach ($f in $viejos) {
        $total++; $bytes += $f.Length
        if (-not $Simular) { Remove-Item -LiteralPath $f.FullName -Force }
    }
    Write-Bien "$sub : $($viejos.Count) archivo(s)"
}

$mb = [math]::Round($bytes / 1MB, 1)
$verbo = if ($Simular) { 'se borrarían' } else { 'borrados' }
$resumen = "$(Get-Date -Format 's') purga retención=$Dias d · $total archivo(s) $verbo · $mb MB"
Write-Paso $resumen
if (-not $Simular) {
    $log = Join-Path $datos 'logs\purga.log'
    New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
    Add-Content -LiteralPath $log -Value $resumen -Encoding UTF8
}
Write-Aviso 'El CSV placas.csv no se toca: archívalo tú si necesitas rotarlo.'
