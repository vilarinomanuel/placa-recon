<#
.SYNOPSIS
    Funciones comunes de los scripts de placa-recon en Windows.
.DESCRIPTION
    Se carga con dot-sourcing:  . "$PSScriptRoot\comun.ps1"
#>

$ErrorActionPreference = 'Stop'

# Raíz de la instalación: C:\alpr por defecto, o ALPR_HOME si está definida.
function Get-AlprHome {
    if ($env:ALPR_HOME) { return $env:ALPR_HOME }
    $raiz = Split-Path -Parent $PSScriptRoot
    if (Test-Path (Join-Path $raiz 'alpr_stream.py')) { return $raiz }
    return 'C:\alpr'
}

# Estructura única de directorios derivada de la raíz de instalación.
# Debe coincidir con web/web_api.py (Config) y con alpr_stream.py (--output-dir).
function Get-AlprRutas {
    param([string]$Raiz = (Get-AlprHome))
    $datos = Join-Path $Raiz 'datos'
    return [ordered]@{
        Raiz     = $Raiz
        Datos    = $datos
        Config   = Join-Path $Raiz 'config'
        Respaldo = Join-Path $Raiz 'respaldo'
        Venv     = Join-Path $Raiz 'venv'
        Python   = Join-Path $Raiz 'venv\Scripts\python.exe'
        Motor    = Join-Path $Raiz 'alpr_stream.py'
        Csv      = Join-Path $datos 'placas.csv'
        Camaras  = Join-Path $datos 'camaras.json'
        Crops    = Join-Path $datos 'crops'
        Video    = Join-Path $datos 'video'
        Logs     = Join-Path $datos 'logs'
        Run      = Join-Path $datos 'run'
        Live     = Join-Path $datos 'live'
        PanelEnv = Join-Path $Raiz 'config\panel.env'
        BaseEnv  = Join-Path $Raiz 'config\alpr.env'
    }
}

# Publica la raíz en el entorno del proceso para que Python derive las mismas rutas.
function Set-AlprEntorno {
    param([string]$Raiz = (Get-AlprHome))
    $env:ALPR_HOME = $Raiz
    return Get-AlprRutas -Raiz $Raiz
}

function Write-Paso   { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Aviso  { param([string]$m) Write-Host "  ! $m" -ForegroundColor Yellow }
function Write-Bien   { param([string]$m) Write-Host "  + $m" -ForegroundColor Green }
function Write-Fallo  { param([string]$m) Write-Host "  x $m" -ForegroundColor Red }

# Carga un archivo KEY=valor como variables de entorno del proceso actual.
function Import-EnvFile {
    param([Parameter(Mandatory)][string]$Ruta)
    if (-not (Test-Path $Ruta)) {
        Write-Aviso "No existe $Ruta; se usan los valores por defecto"
        return 0
    }
    $n = 0
    foreach ($linea in Get-Content -LiteralPath $Ruta -Encoding UTF8) {
        $t = $linea.Trim()
        if (-not $t -or $t.StartsWith('#') -or ($t -notmatch '=')) { continue }
        $k, $v = $t -split '=', 2
        $k = $k.Trim()
        $v = $v.Trim().Trim('"').Trim("'")
        if ($k) { [Environment]::SetEnvironmentVariable($k, $v, 'Process'); $n++ }
    }
    Write-Bien "$n variables cargadas de $Ruta"
    return $n
}

# Intérprete del venv, con comprobación explícita.
function Get-AlprPython {
    param([string]$Raiz = (Get-AlprHome))
    $py = Join-Path $Raiz 'venv\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        throw "No encuentro el entorno virtual en $py. Crea el venv siguiendo docs\INSTALACION-WINDOWS.md"
    }
    return $py
}

function Test-Administrador {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Administrador {
    if (-not (Test-Administrador)) {
        throw 'Este script necesita una consola de PowerShell abierta como Administrador.'
    }
}

# Restringe un archivo al usuario actual, SYSTEM y Administradores.
function Protect-Archivo {
    param([Parameter(Mandatory)][string]$Ruta)
    if (-not (Test-Path $Ruta)) { return }
    & icacls.exe $Ruta /inheritance:r `
        /grant:r "$($env:USERNAME):(R,W)" `
        /grant:r 'SYSTEM:(R,W)' `
        /grant:r 'BUILTIN\Administradores:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        # Sistemas en inglés usan otro nombre para el grupo.
        & icacls.exe $Ruta /grant:r 'BUILTIN\Administrators:(F)' | Out-Null
    }
    Write-Bien "Permisos restringidos en $Ruta"
}
