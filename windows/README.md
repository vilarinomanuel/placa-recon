# Ejecución en Windows vía web local

Archivos de apoyo para administrar todo el ALPR desde el panel web en un PC con
Windows, sin systemd. Se asume la instalación en `C:\alpr` descrita en
[docs/INSTALACION-WINDOWS.md](../docs/INSTALACION-WINDOWS.md). Si tu instalación
está en otra ruta, pasa `-Raiz D:\ruta` o define `ALPR_HOME`.

## Puesta en marcha

```powershell
cd C:\alpr
powershell -ExecutionPolicy Bypass -File .\windows\preparar-entorno.ps1
powershell -ExecutionPolicy Bypass -File .\windows\iniciar-panel.ps1
```

Para actualizar una instalación ya existente a la última versión del repositorio:

```powershell
powershell -ExecutionPolicy Bypass -File C:\alpr\windows\actualizar.ps1
```

El panel queda en <http://127.0.0.1:8080/>. Desde ahí se añaden cámaras, se
inician y detienen, se edita su `.env` y se consulta su registro en vivo.

## Archivos

| Archivo                     | Para qué sirve                                                                   |
| --------------------------- | -------------------------------------------------------------------------------- |
| `panel.env.example`         | Plantilla de `C:\alpr\config\panel.env`: rutas, backend, control y reinicios      |
| `preparar-entorno.ps1`      | Crea directorios, copia plantillas, protege permisos y verifica dependencias      |
| `iniciar-panel.ps1`         | Arranca el panel (carga `panel.env`, comprueba el puerto, abre el navegador)      |
| `iniciar-camara.ps1`        | Ejecuta una cámara en primer plano para diagnosticar                              |
| `instalar-tarea-panel.ps1`  | Tarea programada al iniciar sesión — **opción recomendada con cámaras USB**       |
| `instalar-panel-nssm.ps1`   | Servicio de Windows con NSSM (solo RTSP: SYSTEM no accede a cámaras USB)          |
| `actualizar.ps1`            | Actualiza el código desde GitHub conservando `config\` y `datos\`, y reinicia el panel |
| `abrir-firewall.ps1`        | Abre el puerto del panel solo en el perfil de red Privado                         |
| `purgar-datos.ps1`          | Retención: borra recortes, videos y registros antiguos; puede instalarse como tarea |
| `comun.ps1`                 | Funciones compartidas (carga de `.env`, permisos, comprobaciones)                 |

## Estructura que se crea en `C:\alpr`

```
C:\alpr
├─ alpr_stream.py, video_source.py, web\
├─ venv\                    entorno virtual de Python
├─ config\
│  ├─ panel.env             configuración del panel (permisos restringidos)
│  ├─ alpr.env              valores comunes del motor
│  └─ <id>.env              una por cámara, generada desde el panel
└─ datos\
   ├─ placas.csv            detecciones
   ├─ camaras.json          almacén de cámaras del panel
   ├─ crops\                recortes por placa y frames completos
   ├─ video\                <id>.mp4 anotado, uno por cámara
   ├─ live\                 <id>.jpg de la vista en vivo del panel
   ├─ logs\                 <id>.log por cámara y panel.*.log
   └─ run\                  archivos PID para reengancharse tras reiniciar el panel
```

Una sola raíz manda: `ALPR_HOME` (o el parámetro `-Raiz` de cualquier script) fija
`C:\alpr`, y de ahí se derivan `config\`, `datos\`, `venv\` y `respaldo\`. El motor
recibe la raíz de datos en `ALPR_OUTPUT_DIR` y resuelve dentro `video\`, `crops\`,
`live\` y `placas.csv`, así que panel y motor escriben siempre en el mismo sitio.
`comun.ps1` expone `Get-AlprRutas` con esa estructura para todos los scripts.

## Cómo se ejecutan las cámaras

Con `ALPR_WEB_BACKEND=proceso` el panel lanza un proceso `alpr_stream.py` por
cámara, con su entorno propio, y lo vigila:

- reinicio automático con espera progresiva (5, 5, 10, 20, 30, 60 s) mientras
  `ALPR_WEB_AUTORESTART=true`;
- el PID se guarda en `datos\run\<id>.pid.json`, así que al reiniciar el panel se
  reengancha a los procesos que siguen vivos en lugar de duplicarlos;
- al detener se cierra el árbol de procesos completo (`taskkill /T /F`);
- la salida se acumula en `datos\logs\<id>.log`, visible desde el panel.

## Notas de seguridad

- `panel.env` y `alpr.env` llevan credenciales RTSP: `preparar-entorno.ps1` les
  aplica `icacls` para dejarlos accesibles solo a tu usuario, SYSTEM y
  Administradores.
- Deja `ALPR_WEB_HOST=127.0.0.1` salvo que necesites acceder desde otro equipo de
  la red interna; en ese caso usa `0.0.0.0` **y** `abrir-firewall.ps1 -Origen`
  con tu subred.
- El panel no tiene autenticación propia y muestra matrículas, que son datos
  personales: no lo expongas a Internet sin un proxy inverso con TLS y
  contraseña, y define una política de retención con `purgar-datos.ps1`.
