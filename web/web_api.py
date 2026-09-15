#!/usr/bin/env python3
"""API del panel web de placa-recon.

Expone en JSON el estado del reconocimiento de placas y la administración de
cámaras. Es de solo lectura sobre los datos que produce ``alpr_stream.py``
(CSV, recortes JPEG y video anotado) y opcionalmente controla la ejecución de
las cámaras con dos backends intercambiables:

* ``systemd``  — unidades ``alpr-stream@<id>.service`` (Linux con systemd).
* ``proceso``  — supervisor propio que lanza y vigila ``alpr_stream.py``
  directamente, con reinicio automático. Es el modo usado en Windows, macOS y
  Termux/proot, donde no hay systemd.

Configuración por entorno (todas opcionales):

    ALPR_WEB_DATA           Directorio de datos            (/var/lib/alpr)
    ALPR_WEB_CSV            Ruta del CSV                   (<datos>/placas.csv)
    ALPR_WEB_CAMERAS        Almacén de cámaras JSON        (<datos>/camaras.json)
    ALPR_WEB_UNIT           Plantilla de unidad systemd    (alpr-stream@)
    ALPR_WEB_ALLOW_CONTROL  Permitir iniciar/detener       (false)
    ALPR_WEB_BACKEND        auto | systemd | proceso       (auto)
    ALPR_WEB_CONFIG         Directorio de archivos .env    (<datos>/config)
    ALPR_WEB_LOGS           Directorio de registros        (<datos>/logs)
    ALPR_WEB_RUN            Directorio de archivos PID     (<datos>/run)
    ALPR_WEB_PYTHON         Intérprete para los procesos   (el del panel)
    ALPR_WEB_SCRIPT         Ruta de alpr_stream.py         (raíz del proyecto)
    ALPR_WEB_BASE_ENV       .env base común a las cámaras  (<config>/alpr.env)
    ALPR_WEB_AUTORESTART    Reiniciar procesos caídos      (true)
    ALPR_WEB_MAX_RESTARTS   Límite de reinicios, 0 = sin   (0)
    ALPR_WEB_HOST           Interfaz de escucha            (127.0.0.1)
    ALPR_WEB_PORT           Puerto                         (8080)
    ALPR_WEB_DEMO           Datos sintéticos de demo       (false)

Uso:
    python web/web_api.py                # sirve API + panel en el puerto 8080
    python web/web_api.py --demo         # con datos de demostración
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from supervisor import Supervisor, python_del_entorno

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
STATIC_DIR = BASE_DIR / "static"

# Permite importar video_source.py desde la raíz del proyecto.
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

LOG = logging.getLogger("alpr.web")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #
def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


class Config:
    """Rutas y permisos efectivos del panel."""

    def __init__(self) -> None:
        self.demo = _bool_env("ALPR_WEB_DEMO")
        # --- una sola raíz de instalación (ALPR_HOME) de la que cuelga todo ------
        # Windows y Termux: <home>/datos y <home>/config.
        # Linux con instalación de sistema (/opt/alpr): /var/lib/alpr y /etc/alpr.
        self.home = Path(os.environ.get("ALPR_HOME", str(PROJECT_DIR))).expanduser()
        sistema = os.name != "nt" and str(self.home) == "/opt/alpr"
        if self.demo:
            default_data, default_config = BASE_DIR / "demo-datos", BASE_DIR / "demo-datos" / "config"
        elif sistema:
            default_data, default_config = Path("/var/lib/alpr"), Path("/etc/alpr")
        else:
            default_data, default_config = self.home / "datos", self.home / "config"
        self.data_dir = Path(os.environ.get("ALPR_WEB_DATA", str(default_data))).expanduser()
        self.csv_path = Path(
            os.environ.get("ALPR_WEB_CSV", str(self.data_dir / "placas.csv"))
        ).expanduser()
        self.cameras_path = Path(
            os.environ.get("ALPR_WEB_CAMERAS", str(self.data_dir / "camaras.json"))
        ).expanduser()
        self.unit_template = os.environ.get("ALPR_WEB_UNIT", "alpr-stream@")
        self.allow_control = _bool_env("ALPR_WEB_ALLOW_CONTROL", self.demo)
        self.host = os.environ.get("ALPR_WEB_HOST", "127.0.0.1")
        self.port = int(os.environ.get("ALPR_WEB_PORT", "8080"))

        # --- backend de ejecución y rutas del supervisor de procesos
        self.backend = os.environ.get("ALPR_WEB_BACKEND", "auto").strip().lower()
        if self.backend not in {"auto", "systemd", "proceso"}:
            self.backend = "auto"
        self.config_dir = Path(
            os.environ.get("ALPR_WEB_CONFIG", str(default_config))
        ).expanduser()
        self.log_dir = Path(
            os.environ.get("ALPR_WEB_LOGS", str(self.data_dir / "logs"))
        ).expanduser()
        self.run_dir = Path(
            os.environ.get("ALPR_WEB_RUN", str(self.data_dir / "run"))
        ).expanduser()
        # Vista en vivo: el motor publica aquí un JPEG por cámara.
        self.live_dir = Path(
            os.environ.get("ALPR_WEB_LIVE", str(self.data_dir / "live"))
        ).expanduser()
        # Video anotado por cámara: <datos>/video/<id>.mp4
        self.video_dir = Path(
            os.environ.get("ALPR_WEB_VIDEO", str(self.data_dir / "video"))
        ).expanduser()
        self.crops_dir = Path(
            os.environ.get("ALPR_WEB_CROPS", str(self.data_dir / "crops"))
        ).expanduser()
        self.live_fps = max(0.2, float(os.environ.get("ALPR_WEB_LIVE_FPS", "3")))
        self.live_width = max(160, int(os.environ.get("ALPR_WEB_LIVE_WIDTH", "640")))
        self.live_quality = min(100, max(1, int(os.environ.get("ALPR_WEB_LIVE_QUALITY", "70"))))
        # Segundos sin fotograma nuevo tras los cuales se considera sin señal.
        self.live_max_age = max(2.0, float(os.environ.get("ALPR_WEB_LIVE_MAX_AGE", "12")))
        self.python_exe = python_del_entorno()
        self.script = Path(
            os.environ.get("ALPR_WEB_SCRIPT", str(PROJECT_DIR / "alpr_stream.py"))
        ).expanduser()
        self.base_env = Path(
            os.environ.get("ALPR_WEB_BASE_ENV", str(self.config_dir / "alpr.env"))
        ).expanduser()
        self.autoreiniciar = _bool_env("ALPR_WEB_AUTORESTART", True)
        self.max_reinicios = int(os.environ.get("ALPR_WEB_MAX_RESTARTS", "0"))

    def crear_directorios(self) -> None:
        for carpeta in (self.data_dir, self.config_dir, self.log_dir, self.run_dir,
                        self.live_dir, self.video_dir, self.crops_dir):
            try:
                carpeta.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                LOG.warning("No pude crear %s: %s", carpeta, exc)

    def live_path(self, camera_id: str) -> Path:
        """JPEG de la vista en vivo de una cámara."""
        return self.live_dir / f"{camera_id}.jpg"

    def unit_name(self, camera_id: str) -> str:
        return f"{self.unit_template}{camera_id}.service"

    def as_dict(self) -> dict[str, Any]:
        return {
            "modo_demo": self.demo,
            "directorio_datos": str(self.data_dir),
            "csv": str(self.csv_path),
            "csv_existe": self.csv_path.is_file(),
            "almacen_camaras": str(self.cameras_path),
            "plantilla_unidad": f"{self.unit_template}<id>.service",
            "control_habilitado": self.allow_control,
            "backend": backend_activo(),
            "backend_solicitado": self.backend,
            "directorio_config": str(self.config_dir),
            "directorio_logs": str(self.log_dir),
            "directorio_raiz": str(self.home),
            "directorio_run": str(self.run_dir),
            "directorio_video": str(self.video_dir),
            "directorio_recortes": str(self.crops_dir),
            "directorio_vista": str(self.live_dir),
            "vista_fps": self.live_fps,
            "vista_ancho": self.live_width,
            "vista_max_edad": self.live_max_age,
            "env_base": str(self.base_env),
            "env_base_existe": self.base_env.is_file(),
            "python": self.python_exe,
            "motor": str(self.script),
            "motor_existe": self.script.is_file(),
            "autoreinicio": self.autoreiniciar,
            "max_reinicios": self.max_reinicios,
        }


CFG = Config()


# --------------------------------------------------------------------------- #
# Modelos
# --------------------------------------------------------------------------- #
class CameraIn(BaseModel):
    """Cámara declarada por el usuario en el panel."""

    nombre: str = Field(min_length=1, max_length=64)
    fuente: str = Field(min_length=1, max_length=512)
    tipo: str = Field(default="rtsp")
    min_confianza: float = Field(default=0.8, ge=0.0, le=1.0)
    fps_objetivo: float = Field(default=8.0, ge=0.0, le=60.0)
    activa: bool = True
    notas: str = Field(default="", max_length=512)

    @field_validator("tipo")
    @classmethod
    def _tipo_valido(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"rtsp", "usb", "archivo", "http"}:
            raise ValueError("tipo debe ser rtsp, usb, archivo o http")
        return v

    @field_validator("fuente")
    @classmethod
    def _sin_saltos(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("la fuente no puede contener saltos de línea")
        return v.strip()


class EnvIn(BaseModel):
    """Contenido de un archivo .env enviado desde el panel."""

    contenido: str = Field(default="", max_length=8192)


class CameraAction(BaseModel):
    accion: str

    @field_validator("accion")
    @classmethod
    def _accion_valida(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"iniciar", "detener", "reiniciar"}:
            raise ValueError("accion debe ser iniciar, detener o reiniciar")
        return v


def redact(texto: str) -> str:
    """Oculta credenciales embebidas en una URL antes de exponerla."""
    return re.sub(r"://([^/@\s:]+):([^/@\s]+)@", r"://\1:***@", texto or "")


def slugify(nombre: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", nombre.strip().lower()).strip("-")
    return (base or "camara")[:32]


# --------------------------------------------------------------------------- #
# Almacén de cámaras (JSON en disco, escritura atómica)
# --------------------------------------------------------------------------- #
class CameraStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: list[dict[str, Any]] | None = None

    def load(self) -> list[dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        if not self.path.is_file():
            self._cache = []
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = data if isinstance(data, list) else list(data.get("camaras", []))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("No pude leer %s: %s", self.path, exc)
            self._cache = []
        return self._cache

    def save(self, camaras: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(camaras, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._cache = camaras

    def get(self, camera_id: str) -> dict[str, Any] | None:
        return next((c for c in self.load() if c["id"] == camera_id), None)

    def add(self, datos: CameraIn) -> dict[str, Any]:
        camaras = list(self.load())
        base = slugify(datos.nombre)
        existentes = {c["id"] for c in camaras}
        camera_id, n = base, 2
        while camera_id in existentes:
            camera_id, n = f"{base}-{n}", n + 1
        nueva = {"id": camera_id, "creada": datetime.now().astimezone().isoformat(timespec="seconds"),
                 **datos.model_dump()}
        camaras.append(nueva)
        self.save(camaras)
        LOG.info("Cámara añadida: %s (%s)", camera_id, redact(nueva["fuente"]))
        return nueva

    def update(self, camera_id: str, datos: CameraIn) -> dict[str, Any] | None:
        camaras = list(self.load())
        for i, c in enumerate(camaras):
            if c["id"] == camera_id:
                camaras[i] = {**c, **datos.model_dump()}
                self.save(camaras)
                LOG.info("Cámara actualizada: %s", camera_id)
                return camaras[i]
        return None

    def delete(self, camera_id: str) -> bool:
        camaras = self.load()
        quedan = [c for c in camaras if c["id"] != camera_id]
        if len(quedan) == len(camaras):
            return False
        self.save(quedan)
        LOG.info("Cámara eliminada: %s", camera_id)
        return True


STORE = CameraStore(CFG.cameras_path)


# --------------------------------------------------------------------------- #
# Backend de ejecución: systemd o supervisor de procesos
# --------------------------------------------------------------------------- #
def _systemctl_disponible() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()


def backend_activo() -> str:
    """Backend efectivo: ``simulado`` en demo, ``systemd`` si lo hay, si no ``proceso``."""
    if CFG.demo:
        return "simulado"
    if CFG.backend == "systemd":
        return "systemd" if _systemctl_disponible() else "simulado"
    if CFG.backend == "proceso":
        return "proceso"
    return "systemd" if _systemctl_disponible() else "proceso"


def construir_supervisor() -> Supervisor:
    return Supervisor(
        python_exe=CFG.python_exe,
        script=CFG.script,
        project_dir=PROJECT_DIR,
        data_dir=CFG.data_dir,
        config_dir=CFG.config_dir,
        log_dir=CFG.log_dir,
        run_dir=CFG.run_dir,
        csv_file=CFG.csv_path,
        video_dir=CFG.video_dir,
        crops_dir=CFG.crops_dir,
        live_dir=CFG.live_dir,
        live_fps=CFG.live_fps,
        live_width=CFG.live_width,
        live_quality=CFG.live_quality,
        base_env_file=CFG.base_env,
        autoreiniciar=CFG.autoreiniciar,
        max_reinicios=CFG.max_reinicios,
    )


SUP = construir_supervisor()


def unit_status(camera_id: str) -> dict[str, Any]:
    """Estado de la unidad systemd de una cámara, sin requerir privilegios."""
    unidad = CFG.unit_name(camera_id)
    if backend_activo() == "proceso":
        return SUP.estado(camera_id)
    if not _systemctl_disponible():
        return {"unidad": unidad, "disponible": False, "estado": "desconocido",
                "activa": False, "detalle": "systemd no disponible en este host"}
    try:
        out = subprocess.run(
            ["systemctl", "show", unidad, "--no-pager", "--property",
             "ActiveState,SubState,Result,NRestarts,ExecMainStartTimestamp,LoadState"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"unidad": unidad, "disponible": False, "estado": "error",
                "activa": False, "detalle": str(exc)}
    props = dict(
        line.split("=", 1) for line in out.strip().splitlines() if "=" in line
    )
    estado = props.get("ActiveState", "desconocido")
    inicio = props.get("ExecMainStartTimestamp") or ""
    return {
        "unidad": unidad,
        "disponible": props.get("LoadState") == "loaded",
        "estado": estado,
        "subestado": props.get("SubState", ""),
        "activa": estado == "active",
        "reinicios": int(props.get("NRestarts") or 0),
        "desde": inicio,
        "detalle": props.get("Result", ""),
    }


def unit_action(camera_id: str, accion: str) -> dict[str, Any]:
    """Ejecuta start/stop/restart. Requiere ALPR_WEB_ALLOW_CONTROL=true."""
    if not CFG.allow_control:
        raise HTTPException(
            status_code=403,
            detail="Control deshabilitado. Activa ALPR_WEB_ALLOW_CONTROL=true y "
                   "concede una regla de polkit o sudo para systemctl.",
        )
    backend = backend_activo()
    if backend == "proceso":
        camara = STORE.get(camera_id)
        if camara is None:
            raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
        SUP.registrar(STORE.load())
        try:
            if accion == "iniciar":
                res = SUP.iniciar(camara)
            elif accion == "detener":
                res = SUP.detener(camera_id)
            else:
                res = SUP.reiniciar(camara)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        LOG.info("Acción %s aplicada a %s (backend proceso)", accion, camera_id)
        return {**res, "simulado": False, "backend": "proceso"}
    if backend == "simulado":
        DEMO_UNITS[camera_id] = accion != "detener"
        return {"ok": True, "simulado": True, "accion": accion,
                "detalle": "sin backend de ejecución: acción simulada"}
    mapa = {"iniciar": "start", "detener": "stop", "reiniciar": "restart"}
    cmd = ["systemctl", mapa[accion], CFG.unit_name(camera_id)]
    if os.geteuid() != 0 and shutil.which("sudo"):
        cmd = ["sudo", "-n", *cmd]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    if proc.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=f"systemctl {mapa[accion]} falló ({proc.returncode}): "
                   f"{(proc.stderr or proc.stdout).strip()[:300]}",
        )
    LOG.info("Acción %s aplicada a %s (backend systemd)", accion, camera_id)
    return {"ok": True, "simulado": False, "accion": accion, "detalle": "aplicado",
            "backend": "systemd"}


DEMO_UNITS: dict[str, bool] = {}


def estado_vista(camera_id: str) -> dict[str, Any]:
    """Disponibilidad y antigüedad del último fotograma publicado por el motor."""
    ruta = CFG.live_path(camera_id)
    try:
        edad = max(0.0, time.time() - ruta.stat().st_mtime)
    except OSError:
        return {"disponible": False, "edad_s": None, "fresca": False,
                "max_edad_s": CFG.live_max_age}
    return {
        "disponible": True,
        "edad_s": round(edad, 1),
        # En demostración los fotogramas son fijos: se muestran como frescos.
        "fresca": CFG.demo or edad <= CFG.live_max_age,
        "max_edad_s": CFG.live_max_age,
    }


def estado_camara(camara: dict[str, Any], detecciones_por_camara: Counter) -> dict[str, Any]:
    backend = backend_activo()
    if backend == "proceso":
        servicio = SUP.estado(camara["id"])
    elif backend == "simulado":
        activa = DEMO_UNITS.get(camara["id"], bool(camara.get("activa", True)))
        servicio = {
            "unidad": CFG.unit_name(camara["id"]),
            "disponible": CFG.demo,
            "estado": "active" if activa else "inactive",
            "subestado": "running" if activa else "dead",
            "activa": activa,
            "reinicios": 0,
            "desde": "",
            "detalle": "simulado" if CFG.demo else "sin backend de ejecución",
            "backend": "simulado",
        }
    else:
        servicio = unit_status(camara["id"])
    salida = {**camara, "fuente": redact(camara["fuente"]), "servicio": servicio,
              "detecciones": detecciones_por_camara.get(camara["id"], 0),
              "vista": estado_vista(camara["id"])}
    return salida


# --------------------------------------------------------------------------- #
# Lectura del CSV de detecciones
# --------------------------------------------------------------------------- #
def _num(valor: Any, defecto: float = 0.0) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


def _camara_de_fuente(fuente: str) -> str:
    """Deriva el id de cámara a partir del campo ``source`` del CSV."""
    fuente = (fuente or "").strip()
    for c in STORE.load():
        if c["fuente"] and (c["fuente"] in fuente or fuente in c["fuente"]):
            return c["id"]
        if c["id"] and c["id"] in fuente:
            return c["id"]
    return fuente or "desconocida"


class Detections:
    """Cache del CSV, invalidada por tamaño y fecha de modificación."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: list[dict[str, Any]] = []
        self._sig: tuple[int, float] | None = None

    def _signature(self) -> tuple[int, float] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime)

    def rows(self) -> list[dict[str, Any]]:
        sig = self._signature()
        if sig is None:
            self._rows, self._sig = [], None
            return self._rows
        if sig == self._sig:
            return self._rows
        filas: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8", newline="") as fh:
                for i, raw in enumerate(csv.DictReader(fh)):
                    filas.append(self._normalizar(raw, i))
        except OSError as exc:
            LOG.error("No pude leer el CSV %s: %s", self.path, exc)
            return self._rows
        filas.sort(key=lambda r: r["momento"] or "", reverse=True)
        self._rows, self._sig = filas, sig
        LOG.debug("CSV recargado: %d filas", len(filas))
        return filas

    @staticmethod
    def _normalizar(raw: dict[str, str], indice: int) -> dict[str, Any]:
        ocr, det = _num(raw.get("ocr_confidence")), _num(raw.get("detection_confidence"))
        return {
            "id": indice,
            "placa": (raw.get("plate") or "").strip().upper(),
            "frame": int(_num(raw.get("frame_id"))),
            "momento": (raw.get("wallclock_local") or "").strip(),
            "tiempo_stream": raw.get("stream_timestamp_hms") or "",
            "fuente": redact(raw.get("source") or ""),
            # El motor escribe camera_id cuando lo lanza el panel; si el CSV es
            # antiguo o se ejecutó a mano, se deduce de la fuente.
            "camara": (raw.get("camera_id") or "").strip() or _camara_de_fuente(raw.get("source") or ""),
            "confianza": round(min(ocr, det) if det else ocr, 4),
            "confianza_ocr": round(ocr, 4),
            "confianza_deteccion": round(det, 4),
            "recorte": raw.get("crop_path") or "",
            "frame_completo": raw.get("frame_path") or "",
            "caja": [int(_num(raw.get(k))) for k in ("x1", "y1", "x2", "y2")],
        }


DETS = Detections(CFG.csv_path)


def _parse_momento(valor: str) -> datetime | None:
    try:
        return datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        return None


def filtrar(
    filas: Iterable[dict[str, Any]],
    placa: str = "",
    camara: str = "",
    min_confianza: float = 0.0,
    desde: str = "",
    hasta: str = "",
) -> list[dict[str, Any]]:
    placa = placa.strip().upper()
    d_desde, d_hasta = _parse_momento(desde), _parse_momento(hasta)
    salida = []
    for f in filas:
        if placa and placa not in f["placa"]:
            continue
        if camara and f["camara"] != camara:
            continue
        if f["confianza"] < min_confianza:
            continue
        if d_desde or d_hasta:
            m = _parse_momento(f["momento"])
            if m is None:
                continue
            if d_desde and m < d_desde:
                continue
            if d_hasta and m > d_hasta:
                continue
        salida.append(f)
    return salida


# --------------------------------------------------------------------------- #
# Aplicación
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _ciclo_vida(_: FastAPI):
    """Prepara el backend al arrancar y cierra la vigilancia al salir."""
    _al_arrancar()
    try:
        yield
    finally:
        _al_apagar()


app = FastAPI(title="placa-recon · panel", docs_url="/api/docs", redoc_url=None,
              lifespan=_ciclo_vida)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.exception_handler(ValueError)
async def _valor_invalido(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def _al_arrancar() -> None:
    """Prepara directorios y adopta procesos que sobrevivieron al panel."""
    if CFG.demo:
        LOG.info("Modo demostración: el control de cámaras es simulado")
        return
    CFG.crear_directorios()
    if backend_activo() != "proceso":
        return
    camaras = STORE.load()
    adoptadas = SUP.reenganchar(camaras)
    LOG.info(
        "Backend de procesos activo (%d cámara(s) reenganchada(s), autoreinicio=%s)",
        adoptadas, CFG.autoreiniciar,
    )
    for camara in camaras:
        if camara.get("activa") and not SUP.estado(camara["id"])["activa"] and CFG.allow_control:
            try:
                SUP.iniciar(camara)
            except Exception as exc:  # noqa: BLE001
                LOG.error("Arranque automático de %s falló: %s", camara["id"], exc)


def _al_apagar() -> None:
    """Detiene la vigilancia; los procesos ALPR siguen vivos y se reenganchan luego."""
    SUP.apagar(detener_procesos=_bool_env("ALPR_WEB_STOP_ON_EXIT", False))


@app.get("/api/salud")
def salud() -> dict[str, Any]:
    return {"ok": True, "hora": datetime.now().astimezone().isoformat(timespec="seconds")}


@app.get("/api/estado")
def estado() -> dict[str, Any]:
    filas = DETS.rows()
    ahora = datetime.now().astimezone()
    por_camara: Counter = Counter(f["camara"] for f in filas)
    ultima_hora = [
        f for f in filas
        if (m := _parse_momento(f["momento"])) and (ahora - m) <= timedelta(hours=1)
    ]
    hoy = [
        f for f in filas
        if (m := _parse_momento(f["momento"])) and m.date() == ahora.date()
    ]
    camaras = [estado_camara(c, por_camara) for c in STORE.load()]
    activas = sum(1 for c in camaras if c["servicio"]["activa"])

    try:
        from video_source import detect_platform  # noqa: PLC0415

        plataforma = detect_platform()
    except Exception as exc:  # noqa: BLE001
        plataforma = "desconocida"
        LOG.debug("No pude detectar la plataforma: %s", exc)

    uso = None
    try:
        du = shutil.disk_usage(CFG.data_dir if CFG.data_dir.exists() else BASE_DIR)
        uso = {"total_gb": round(du.total / 2**30, 1), "libre_gb": round(du.free / 2**30, 1),
               "usado_pct": round(100 * (du.total - du.free) / du.total, 1)}
    except OSError:
        pass

    return {
        "config": CFG.as_dict(),
        "plataforma": plataforma,
        "camaras_totales": len(camaras),
        "camaras_activas": activas,
        "detecciones_totales": len(filas),
        "detecciones_hoy": len(hoy),
        "detecciones_ultima_hora": len(ultima_hora),
        "placas_unicas": len({f["placa"] for f in filas}),
        "confianza_media": round(
            sum(f["confianza"] for f in filas) / len(filas), 4
        ) if filas else 0.0,
        "ultima_deteccion": filas[0] if filas else None,
        "almacenamiento": uso,
        "camaras": camaras,
        "hora_servidor": ahora.isoformat(timespec="seconds"),
    }


@app.get("/api/camaras")
def listar_camaras() -> dict[str, Any]:
    por_camara: Counter = Counter(f["camara"] for f in DETS.rows())
    return {"camaras": [estado_camara(c, por_camara) for c in STORE.load()],
            "control_habilitado": CFG.allow_control}


@app.post("/api/camaras", status_code=201)
def crear_camara(datos: CameraIn) -> dict[str, Any]:
    if len(STORE.load()) >= 64:
        raise HTTPException(status_code=409, detail="Límite de 64 cámaras alcanzado")
    return estado_camara(STORE.add(datos), Counter())


@app.put("/api/camaras/{camera_id}")
def editar_camara(camera_id: str, datos: CameraIn) -> dict[str, Any]:
    if not SLUG_RE.match(camera_id):
        raise HTTPException(status_code=400, detail="Identificador de cámara inválido")
    actualizada = STORE.update(camera_id, datos)
    if actualizada is None:
        raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
    return estado_camara(actualizada, Counter())


@app.delete("/api/camaras/{camera_id}")
def eliminar_camara(camera_id: str) -> dict[str, Any]:
    if not STORE.delete(camera_id):
        raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
    DEMO_UNITS.pop(camera_id, None)
    return {"eliminada": camera_id}


@app.post("/api/camaras/{camera_id}/accion")
def accion_camara(camera_id: str, cuerpo: CameraAction) -> dict[str, Any]:
    if STORE.get(camera_id) is None:
        raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
    resultado = unit_action(camera_id, cuerpo.accion)
    por_camara: Counter = Counter(f["camara"] for f in DETS.rows())
    return {**resultado, "camara": estado_camara(STORE.get(camera_id), por_camara)}


def _contenido_env(camara: dict[str, Any], ocultar: bool = False) -> str:
    """Bloque ALPR_* de una cámara. Con ``ocultar`` enmascara las credenciales."""
    fuente = redact(camara["fuente"]) if ocultar else camara["fuente"]
    lineas = [
        f"# {camara['nombre']} — generado por el panel de placa-recon",
        f"# Cámara {camara['id']} · tipo {camara['tipo']}",
        f"ALPR_CAMERA_ID={camara['id']}",
        f"ALPR_CAMERA_NAME={camara['nombre']}",
        f"ALPR_INPUT={fuente}",
        f"ALPR_MIN_CONFIDENCE={camara['min_confianza']}",
        f"ALPR_TARGET_FPS={camara['fps_objetivo']}",
        "",
        "# Raíz única de datos: las rutas relativas se resuelven dentro de ella",
        f"ALPR_OUTPUT_DIR={CFG.data_dir}",
        f"ALPR_CSV={CFG.csv_path}",
        f"ALPR_OUTPUT={CFG.data_dir / 'video' / (camara['id'] + '.mp4')}",
        f"ALPR_CROPS_DIR={CFG.data_dir / 'crops'}",
        "ALPR_SAVE_CROPS=true",
        "ALPR_SAVE_FULL_FRAME=true",
        "ALPR_HUD=true",
        "",
        "# Vista en vivo que muestra el panel",
        f"ALPR_LIVE_VIEW={CFG.live_path(camara['id'])}",
        f"ALPR_LIVE_VIEW_FPS={CFG.live_fps:g}",
        f"ALPR_LIVE_VIEW_WIDTH={CFG.live_width}",
        f"ALPR_LIVE_VIEW_QUALITY={CFG.live_quality}",
    ]
    if camara["tipo"] == "rtsp":
        lineas.append("ALPR_RTSP_TRANSPORT=tcp")
    return "\n".join(lineas) + "\n"


@app.get("/api/camaras/{camera_id}/env")
def env_camara(camera_id: str) -> dict[str, Any]:
    """Configuración .env de una cámara: contenido generado y archivo en disco."""
    camara = STORE.get(camera_id)
    if camara is None:
        raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
    ruta = CFG.config_dir / f"{camera_id}.env"
    return {
        "camara": camara["id"],
        "ruta": str(ruta),
        "existe": ruta.is_file(),
        "ruta_sugerida": str(ruta),
        "contenido": _contenido_env(camara, ocultar=True),
        "credenciales_ocultas": redact(camara["fuente"]) != camara["fuente"],
        "unidad": CFG.unit_name(camara["id"]),
        "backend": backend_activo(),
        "editable": backend_activo() == "proceso" and CFG.allow_control,
    }


@app.post("/api/camaras/{camera_id}/env")
def guardar_env_camara(camera_id: str, cuerpo: EnvIn | None = None) -> dict[str, Any]:
    """Escribe el .env de la cámara en el directorio de configuración del panel."""
    camara = STORE.get(camera_id)
    if camara is None:
        raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
    if not CFG.allow_control:
        raise HTTPException(
            status_code=403,
            detail="Escritura deshabilitada. Activa ALPR_WEB_ALLOW_CONTROL=true.",
        )
    contenido = (cuerpo.contenido if cuerpo and cuerpo.contenido else _contenido_env(camara))
    try:
        ruta = SUP.escribir_env(camara, contenido)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No pude escribir el archivo: {exc}") from exc
    return {"camara": camera_id, "ruta": str(ruta), "guardado": True}


@app.get("/api/camaras/{camera_id}/registro")
def registro_camara(
    camera_id: str,
    lineas: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Últimas líneas del registro del proceso de una cámara."""
    if STORE.get(camera_id) is None:
        raise HTTPException(status_code=404, detail=f"No existe la cámara {camera_id}")
    datos = SUP.registro(camera_id, lineas=lineas)
    return {"camara": camera_id, "backend": backend_activo(), **datos}


@app.get("/api/camaras-detectadas")
def camaras_detectadas(
    escanear_ip: bool = Query(False, description="Buscar cámaras IP en la subred local"),
) -> dict[str, Any]:
    """Inventario del hardware local usando video_source.enumerate_cameras()."""
    try:
        from video_source import enumerate_cameras  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503, detail=f"video_source.py no disponible: {exc}"
        ) from exc
    try:
        camaras = enumerate_cameras(include_ip=escanear_ip)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Fallo la enumeración de cámaras: %s", exc)
        raise HTTPException(status_code=502, detail=f"Error al enumerar: {exc}") from exc
    return {
        "detectadas": [
            {
                "indice": c.index,
                "nombre": c.name,
                "device_id": str(c.device_id),
                "clase": c.kind,
                "url": redact(c.url or ""),
                "orientacion": c.facing or "",
                "resolucion": f"{c.width}x{c.height}" if c.width and c.height else "",
                "fps": round(c.fps, 1) if c.fps else 0,
                "backend": str(c.backend) if c.backend is not None else "",
                "verificada": bool(c.verified),
                "detalle": c.detail or "",
            }
            for c in camaras
        ],
        "escaneo_ip": escanear_ip,
    }


@app.get("/api/detecciones")
def detecciones(
    placa: str = "",
    camara: str = "",
    min_confianza: float = Query(0.0, ge=0.0, le=1.0),
    desde: str = "",
    hasta: str = "",
    limite: int = Query(50, ge=1, le=500),
    desplazamiento: int = Query(0, ge=0),
) -> dict[str, Any]:
    filas = filtrar(DETS.rows(), placa, camara, min_confianza, desde, hasta)
    ventana = filas[desplazamiento: desplazamiento + limite]
    return {"total": len(filas), "limite": limite, "desplazamiento": desplazamiento,
            "detecciones": ventana}


@app.get("/api/detecciones.csv")
def exportar_csv(
    placa: str = "", camara: str = "", min_confianza: float = Query(0.0, ge=0.0, le=1.0),
    desde: str = "", hasta: str = "",
) -> StreamingResponse:
    filas = filtrar(DETS.rows(), placa, camara, min_confianza, desde, hasta)
    buf = io.StringIO()
    campos = ["placa", "momento", "camara", "fuente", "confianza", "confianza_ocr",
              "confianza_deteccion", "frame", "tiempo_stream", "recorte"]
    w = csv.DictWriter(buf, fieldnames=campos, extrasaction="ignore")
    w.writeheader()
    w.writerows(filas)
    buf.seek(0)
    nombre = f"detecciones-{datetime.now():%Y%m%d-%H%M}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.get("/api/metricas")
def metricas(horas: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
    filas = DETS.rows()
    ahora = datetime.now().astimezone()
    inicio = ahora - timedelta(hours=horas)
    serie: dict[str, int] = {}
    cursor = inicio.replace(minute=0, second=0, microsecond=0)
    while cursor <= ahora:
        serie[cursor.strftime("%Y-%m-%d %H:00")] = 0
        cursor += timedelta(hours=1)

    por_camara: Counter = Counter()
    placas: Counter = Counter()
    histograma = {"0.80-0.85": 0, "0.85-0.90": 0, "0.90-0.95": 0, "0.95-1.00": 0}
    por_camara_hora: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(serie, 0))

    for f in filas:
        m = _parse_momento(f["momento"])
        if m is None or m < inicio:
            continue
        clave = m.strftime("%Y-%m-%d %H:00")
        if clave in serie:
            serie[clave] += 1
            por_camara_hora[f["camara"]][clave] += 1
        por_camara[f["camara"]] += 1
        placas[f["placa"]] += 1
        c = f["confianza"]
        if c < 0.85:
            histograma["0.80-0.85"] += 1
        elif c < 0.90:
            histograma["0.85-0.90"] += 1
        elif c < 0.95:
            histograma["0.90-0.95"] += 1
        else:
            histograma["0.95-1.00"] += 1

    return {
        "horas": horas,
        "por_hora": [{"hora": k, "total": v} for k, v in serie.items()],
        "por_camara": [{"camara": k, "total": v} for k, v in por_camara.most_common()],
        "por_camara_hora": {k: [v[h] for h in serie] for k, v in por_camara_hora.items()},
        "top_placas": [{"placa": k, "total": v} for k, v in placas.most_common(10)],
        "confianza": [{"rango": k, "total": v} for k, v in histograma.items()],
    }


@app.get("/api/imagen")
def imagen(ruta: str) -> FileResponse:
    """Sirve un recorte o frame, restringido al directorio de datos."""
    if not ruta:
        raise HTTPException(status_code=400, detail="Falta el parámetro ruta")
    candidata = Path(ruta)
    if not candidata.is_absolute():
        candidata = CFG.data_dir / candidata
    try:
        resuelta = candidata.resolve(strict=True)
        base = CFG.data_dir.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="Imagen no encontrada") from None
    if not resuelta.is_relative_to(base):
        raise HTTPException(status_code=403, detail="Ruta fuera del directorio de datos")
    if resuelta.suffix.lower() not in IMG_SUFFIXES:
        raise HTTPException(status_code=415, detail="Formato de imagen no admitido")
    return FileResponse(resuelta, media_type="image/jpeg")


@app.get("/api/camaras/{camera_id}/vista.jpg")
def vista_jpeg(camera_id: str) -> Response:
    """Último fotograma anotado de una cámara (JPEG único, sin caché)."""
    STORE.get(camera_id)  # 404 si la cámara no existe
    ruta = CFG.live_path(camera_id)
    try:
        datos = ruta.read_bytes()
        edad = max(0.0, time.time() - ruta.stat().st_mtime)
    except OSError:
        raise HTTPException(
            status_code=404,
            detail="Sin vista en vivo: inicia la cámara o comprueba ALPR_LIVE_VIEW",
        ) from None
    return Response(
        content=datos,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Edad-Segundos": f"{edad:.1f}",
            "X-Fresca": "1" if (CFG.demo or edad <= CFG.live_max_age) else "0",
        },
    )


@app.get("/api/camaras/{camera_id}/vista.mjpeg")
async def vista_mjpeg(camera_id: str, request: Request) -> StreamingResponse:
    """Flujo MJPEG de la vista en vivo, consumible por un <img> del navegador.

    Sigue el archivo publicado por el motor y solo envía un fotograma cuando su
    marca de tiempo cambia, así una cámara detenida no genera tráfico.
    """
    STORE.get(camera_id)
    ruta = CFG.live_path(camera_id)
    limite = 1.0 / CFG.live_fps if CFG.live_fps else 0.0
    frontera = b"--fotograma"

    async def flujo():
        # El delimitador se envía tras cada fotograma para que el navegador lo
        # pinte de inmediato en lugar de esperar el siguiente.
        yield frontera + b"\r\n"
        ultimo_mtime = 0.0
        sin_señal = False
        primero = True  # el primer fotograma se envía siempre, aunque esté congelado
        while True:
            if await request.is_disconnected():
                break
            try:
                mtime = ruta.stat().st_mtime
                edad = max(0.0, time.time() - mtime)
                if mtime != ultimo_mtime and (primero or edad <= CFG.live_max_age):
                    ultimo_mtime = mtime
                    datos = ruta.read_bytes()
                    if datos:
                        sin_señal = False
                        primero = False
                        yield (b"Content-Type: image/jpeg\r\n"
                               + f"Content-Length: {len(datos)}\r\n\r\n".encode()
                               + datos + b"\r\n" + frontera + b"\r\n")
                elif edad > CFG.live_max_age and not sin_señal:
                    sin_señal = True
                    LOG.debug("Vista de %s sin fotogramas nuevos (%.1f s)", camera_id, edad)
            except OSError:
                pass
            await asyncio.sleep(max(0.2, limite))

    return StreamingResponse(
        flujo(),
        media_type="multipart/x-mixed-replace; boundary=fotograma",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/eventos")
async def eventos(request: Request) -> StreamingResponse:
    """SSE: emite las detecciones nuevas a medida que se escriben en el CSV."""

    async def flujo():
        visto = {f["id"] for f in DETS.rows()}
        yield f": conectado {datetime.now():%H:%M:%S}\n\n"
        latido = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            nuevas = [f for f in DETS.rows() if f["id"] not in visto]
            for f in reversed(nuevas):
                visto.add(f["id"])
                yield f"event: deteccion\ndata: {json.dumps(f, ensure_ascii=False)}\n\n"
            if time.monotonic() - latido > 20:
                latido = time.monotonic()
                yield ": latido\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        flujo(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="panel")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Panel web de placa-recon")
    p.add_argument("--host", default=CFG.host)
    p.add_argument("--port", type=int, default=CFG.port)
    p.add_argument("--demo", action="store_true", help="Usar datos de demostración")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.demo:
        os.environ["ALPR_WEB_DEMO"] = "1"
        globals()["CFG"] = Config()
        globals()["STORE"] = CameraStore(CFG.cameras_path)
        globals()["DETS"] = Detections(CFG.csv_path)
        globals()["SUP"] = construir_supervisor()
    CFG.crear_directorios()

    LOG.info("Datos: %s | CSV: %s | control: %s | backend: %s",
             CFG.data_dir, CFG.csv_path,
             "sí" if CFG.allow_control else "no", backend_activo())
    if backend_activo() == "proceso" and not CFG.script.is_file():
        LOG.error("No encuentro el motor ALPR en %s: ajusta ALPR_WEB_SCRIPT", CFG.script)
    if not CFG.csv_path.is_file():
        LOG.warning("El CSV %s no existe todavía: el panel arrancará vacío", CFG.csv_path)

    import uvicorn  # noqa: PLC0415

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
