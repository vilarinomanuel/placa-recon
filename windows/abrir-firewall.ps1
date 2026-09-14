<#
.SYNOPSIS
    Abre el puerto del panel en el firewall solo para la red privada.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File C:\alpr\windows\abrir-firewall.ps1 -Puerto 8080
.EXAMPLE
    .\abrir-firewall.ps1 -Origen 192.168.1.0/24
.EXAMPLE
    .\abrir-firewall.ps1 -Cerrar
#>
[CmdletBinding()]
param(
    [int]$Puerto = 8080,
    [string]$Nombre = 'ALPR panel',
    [string]$Origen = '',
    [switch]$Cerrar
)

. "$PSScriptRoot\comun.ps1"
Assert-Administrador

if ($Cerrar) {
    Remove-NetFirewallRule -DisplayName $Nombre -ErrorAction SilentlyContinue
    Write-Bien "Regla «$Nombre» eliminada"
    exit 0
}

Remove-NetFirewallRule -DisplayName $Nombre -ErrorAction SilentlyContinue
$parametros = @{
    DisplayName = $Nombre
    Direction   = 'Inbound'
    Action      = 'Allow'
    Protocol    = 'TCP'
    LocalPort   = $Puerto
    Profile     = 'Private'
}
if ($Origen) { $parametros.RemoteAddress = $Origen }
New-NetFirewallRule @parametros | Out-Null
Write-Bien "Puerto $Puerto abierto en perfil Privado$(if ($Origen) { " para $Origen" })"
Write-Aviso 'El panel no tiene autenticación: no lo publiques en Internet sin proxy con TLS y contraseña.'
