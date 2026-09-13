#!/usr/bin/env python3
"""
Detección y selección de fuente de video multiplataforma (Android / Windows / Linux / macOS).

Módulo independiente del pipeline de ALPR: expone una `VideoSourceSpec` que
`alpr_stream.py` (o cualquier otro consumidor) abre con OpenCV sin saber de qué
plataforma vino.

Comportamiento por plataforma
-----------------------------
Android
    Selecciona automáticamente la cámara trasera (principal) sin intervención
    del usuario. Si no existe cámara trasera (tablets, dispositivos atípicos)
    cae a la frontal, luego a la primera cámara utilizable y, por último, a un
    sondeo directo de índices de OpenCV. Gestiona los permisos de cámara en
    runtime (API 23+) mediante python-for-android/Kivy, Chaquopy/pyjnius o
    Termux:API, según lo que esté disponible.

PC (Windows / Linux / macOS)
    Presenta un menú con dos opciones claras: procesar un archivo de video
    (explorador gráfico si hay entorno de escritorio, con soporte para unidades
    externas, pendrives y tarjetas SD) o usar una cámara en vivo (webcams
    integradas, USB y cámaras IP descubiertas en la red local), enumerando
    nombre, ID y resolución de cada dispositivo, con vista previa opcional.

Uso
---
    from video_source import select_source, enumerate_cameras, detect_platform

    spec = select_source()          # interactivo en PC, automático en Android
    cap = cv2.VideoCapture(spec.cv_source)

CLI de diagnóstico
------------------
    python video_source.py --list            # enumera cámaras y sale
    python video_source.py --scan-ip         # incluye cámaras IP de la LAN
    python video_source.py --select          # menú interactivo completo
"""

from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import cv2

LOG = logging.getLogger("alpr.source")

# Extensiones de video que aceptamos como archivo de entrada.
VIDEO_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".mpg", ".mpeg", ".wmv",
    ".flv", ".webm", ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".mjpeg", ".mjpg",
)

# Puertos habituales de cámaras IP (RTSP, HTTP/MJPEG, ONVIF).
IP_CAMERA_PORTS = (554, 8554, 8080, 80, 88, 8000)

# Rutas RTSP más comunes por fabricante, para construir una URL sugerida.
COMMON_RTSP_PATHS = (
    "/Streaming/Channels/101",   # Hikvision
    "/cam/realmonitor?channel=1&subtype=0",  # Dahua
    "/live",
    "/live/ch0",
    "/h264Preview_01_main",      # Reolink
    "/stream1",
    "/video1",
    "/onvif1",
    "/",
)


# --------------------------------------------------------------------------- #
# Errores
# --------------------------------------------------------------------------- #


class SourceError(Exception):
    """Error base de selección/apertura de fuente de video."""


class CameraUnavailableError(SourceError):
    """No hay cámaras utilizables o la elegida no entrega frames."""


class CameraPermissionError(SourceError):
    """El sistema operativo denegó el acceso a la cámara."""


class VideoFileError(SourceError):
    """Archivo inexistente, ilegible o con formato no soportado."""


class StorageDisconnectedError(SourceError):
    """El medio que contiene el archivo desapareció durante la lectura."""


class SelectionAborted(SourceError):
    """El usuario canceló la selección."""


# --------------------------------------------------------------------------- #
# Detección de plataforma y entorno
# --------------------------------------------------------------------------- #


def detect_platform() -> str:
    """Devuelve 'android', 'windows', 'linux', 'macos' o 'unknown'."""
    if is_android():
        return "android"
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return "unknown"


def is_android() -> bool:
    """Detección de Android en runtime, robusta ante Termux y apps empaquetadas."""
    if os.environ.get("ALPR_FORCE_PLATFORM", "").lower() == "android":
        return True
    if "ANDROID_ARGUMENT" in os.environ or "P4A_BOOTSTRAP" in os.environ:
        return True  # python-for-android / Kivy
    if os.environ.get("ANDROID_ROOT") and os.environ.get("ANDROID_DATA"):
        return True
    if "com.termux" in os.environ.get("PREFIX", ""):
        return True
    if platform.system().lower() == "linux":
        rel = platform.release().lower()
        if "android" in rel or Path("/system/build.prop").exists():
            return True
        try:
            if "android" in platform.version().lower():
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or shutil.which("termux-camera-info") is not None


def has_gui() -> bool:
    """¿Podemos abrir ventanas (preview / explorador de archivos)?"""
    if os.environ.get("ALPR_NO_GUI", "").lower() in ("1", "true", "yes"):
        return False
    if is_android():
        return False
    system = platform.system().lower()
    if system.startswith("win") or system == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def is_interactive() -> bool:
    """¿Hay una consola donde preguntar? (falso bajo systemd, cron, etc.)"""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:  # noqa: BLE001
        return False


def _run(cmd: Sequence[str], timeout: float = 6.0) -> str:
    """Ejecuta un comando auxiliar y devuelve stdout ('' si falla)."""
    try:
        proc = subprocess.run(  # noqa: S603
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
        if proc.returncode != 0:
            LOG.debug("Comando %s -> rc=%d stderr=%s", cmd[0], proc.returncode, proc.stderr.strip()[:200])
        return proc.stdout
    except FileNotFoundError:
        LOG.debug("Comando no encontrado: %s", cmd[0])
    except subprocess.TimeoutExpired:
        LOG.debug("Comando %s excedió el tiempo límite", cmd[0])
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Comando %s falló: %s", cmd[0], exc)
    return ""


# --------------------------------------------------------------------------- #
# Modelo de datos
# --------------------------------------------------------------------------- #


@dataclass
class CameraInfo:
    """Cámara detectada, local o de red."""

    index: int | None = None            # índice para cv2.VideoCapture
    name: str = "Cámara"
    device_id: str = ""                 # /dev/video0, \\?\usb#..., id de Android
    kind: str = "local"                 # local | ip
    url: str | None = None              # solo cámaras IP
    facing: str = "unknown"             # back | front | external | unknown
    width: int = 0
    height: int = 0
    fps: float = 0.0
    backend: int | None = None          # cv2.CAP_* preferido
    verified: bool = False              # entregó al menos un frame
    detail: str = ""

    @property
    def cv_source(self) -> Any:
        return self.url if self.kind == "ip" else (self.index if self.index is not None else 0)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else "desconocida"

    def describe(self) -> str:
        bits = [self.name]
        ident = self.url or self.device_id or (f"índice {self.index}" if self.index is not None else "")
        if ident:
            bits.append(f"id={ident}")
        bits.append(f"res={self.resolution}")
        if self.fps:
            bits.append(f"{self.fps:.0f}fps")
        if self.facing != "unknown":
            bits.append({"back": "trasera", "front": "frontal", "external": "externa"}.get(self.facing, self.facing))
        if self.detail:
            bits.append(self.detail)
        bits.append("verificada" if self.verified else "sin verificar")
        return " | ".join(bits)


@dataclass
class VideoSourceSpec:
    """Fuente lista para abrir con OpenCV, agnóstica de plataforma."""

    cv_source: Any                       # int, ruta o URL
    kind: str                            # camera | file | ip
    label: str = ""
    is_live: bool = False
    backend: int | None = None
    camera: CameraInfo | None = None
    path: Path | None = None
    platform_name: str = field(default_factory=detect_platform)

    def as_input_string(self) -> str:
        """Representación aceptada por el CLI de alpr_stream.py (-i)."""
        return str(self.cv_source)


# --------------------------------------------------------------------------- #
# Sondeo de cámaras con OpenCV
# --------------------------------------------------------------------------- #


def _backends_for_platform(plat: str) -> list[int | None]:
    """Backends de captura a intentar, en orden de preferencia."""
    def attr(name: str) -> int | None:
        return getattr(cv2, name, None)

    if plat == "windows":
        order = [attr("CAP_DSHOW"), attr("CAP_MSMF"), None]
    elif plat == "macos":
        order = [attr("CAP_AVFOUNDATION"), None]
    elif plat == "android":
        order = [attr("CAP_ANDROID"), None]
    else:
        order = [attr("CAP_V4L2"), None]
    seen: list[int | None] = []
    for b in order:
        if b not in seen:
            seen.append(b)
    return seen


def probe_camera(cam: CameraInfo, plat: str | None = None, warmup: int = 3) -> bool:
    """Abre la cámara, lee un frame y completa resolución/fps. No propaga errores."""
    plat = plat or detect_platform()
    backends = [cam.backend] if cam.backend is not None else _backends_for_platform(plat)
    if cam.kind == "ip":
        backends = [None]
    for backend in backends:
        cap = None
        try:
            src = cam.cv_source
            cap = cv2.VideoCapture(src) if backend is None else cv2.VideoCapture(src, backend)
            if not cap.isOpened():
                LOG.debug("No abre %s (backend=%s)", src, backend)
                continue
            frame = None
            for _ in range(max(warmup, 1)):
                ok, frame = cap.read()
                if ok and frame is not None:
                    break
                time.sleep(0.05)
            if frame is None:
                LOG.debug("Abre pero no entrega frames: %s (backend=%s)", src, backend)
                continue
            h, w = frame.shape[:2]
            cam.width, cam.height = int(w), int(h)
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            cam.fps = fps if 0 < fps <= 1000 else 0.0
            cam.backend = backend
            cam.verified = True
            LOG.debug("Cámara verificada: %s", cam.describe())
            return True
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Error sondeando %s: %s", cam.cv_source, exc)
        finally:
            if cap is not None:
                cap.release()
    return False


# --------------------------------------------------------------------------- #
# Enumeración: Linux
# --------------------------------------------------------------------------- #


def _linux_cameras() -> list[CameraInfo]:
    cams: list[CameraInfo] = []
    nodes = sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
    LOG.debug("Nodos V4L2 encontrados: %s", nodes or "ninguno")

    # Nodos de solo metadatos: los descartamos con v4l2-ctl si está disponible.
    v4l2 = shutil.which("v4l2-ctl")
    for node in nodes:
        idx = int(re.sub(r"\D", "", node) or 0)
        name = f"Cámara V4L2 {idx}"
        detail = ""
        sys_name = Path(f"/sys/class/video4linux/video{idx}/name")
        if sys_name.exists():
            try:
                name = sys_name.read_text(encoding="utf-8", errors="replace").strip() or name
            except OSError as exc:
                LOG.debug("No se pudo leer %s: %s", sys_name, exc)
        if v4l2:
            caps = _run([v4l2, "-d", node, "--all"], timeout=4.0)
            if caps and "Video Capture" not in caps:
                LOG.debug("Descartado %s: sin capacidad de captura de video", node)
                continue
            m = re.search(r"Card type\s*:\s*(.+)", caps)
            if m:
                name = m.group(1).strip()
            if "Metadata Capture" in caps and "Video Capture" not in caps:
                continue
        try:
            if not os.access(node, os.R_OK):
                detail = "sin permiso de lectura (añade tu usuario al grupo 'video')"
                LOG.warning("%s: %s", node, detail)
        except OSError:
            pass
        cams.append(CameraInfo(index=idx, name=name, device_id=node, facing="unknown", detail=detail))
    return cams


# --------------------------------------------------------------------------- #
# Enumeración: Windows
# --------------------------------------------------------------------------- #


def _windows_cameras() -> list[CameraInfo]:
    names: list[str] = []
    try:  # Vía preferida: DirectShow real, mismo orden que usa OpenCV.
        from pygrabber.dshow_graph import FilterGraph  # type: ignore

        names = list(FilterGraph().get_input_devices())
        LOG.debug("pygrabber devolvió %d dispositivos DirectShow", len(names))
    except ImportError:
        LOG.debug("pygrabber no instalado (pip install pygrabber) — uso PowerShell/WMI")
    except Exception as exc:  # noqa: BLE001
        LOG.debug("pygrabber falló: %s", exc)

    if not names:
        ps = (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' -or "
            "$_.Service -eq 'usbvideo' } | Select-Object -ExpandProperty Name"
        )
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=15.0)
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        LOG.debug("WMI devolvió %d cámaras", len(names))

    if not names:  # Último recurso: sondeo ciego de índices.
        LOG.debug("Sin inventario de dispositivos; sondeo ciego de índices 0-5")
        return [CameraInfo(index=i, name=f"Cámara {i}") for i in range(6)]

    return [
        CameraInfo(index=i, name=n, device_id=f"dshow:{i}", facing="unknown")
        for i, n in enumerate(names)
    ]


# --------------------------------------------------------------------------- #
# Enumeración: macOS
# --------------------------------------------------------------------------- #


def _macos_cameras() -> list[CameraInfo]:
    cams: list[CameraInfo] = []
    out = _run(["system_profiler", "-json", "SPCameraDataType"], timeout=20.0)
    if out:
        try:
            data = json.loads(out)
            entries = data.get("SPCameraDataType", []) or []
            for i, entry in enumerate(entries):
                name = entry.get("_name") or f"Cámara {i}"
                uid = entry.get("spcamera_unique-id") or entry.get("spcamera_model-id") or ""
                cams.append(CameraInfo(index=i, name=name, device_id=str(uid), facing="unknown"))
            LOG.debug("system_profiler devolvió %d cámaras", len(cams))
        except (json.JSONDecodeError, AttributeError) as exc:
            LOG.debug("No se pudo interpretar system_profiler: %s", exc)
    if not cams:
        LOG.debug("Sin inventario AVFoundation; sondeo ciego de índices 0-3")
        cams = [CameraInfo(index=i, name=f"Cámara {i}") for i in range(4)]
    return cams


# --------------------------------------------------------------------------- #
# Android: permisos y cámara trasera
# --------------------------------------------------------------------------- #


def request_android_camera_permission(timeout: float = 30.0) -> bool:
    """Solicita el permiso CAMERA en runtime (Android 6 / API 23+).

    Cubre los tres empaquetados habituales:
      1. python-for-android / Kivy  -> android.permissions
      2. Chaquopy / pyjnius nativo  -> ActivityCompat.requestPermissions
      3. Termux:API                 -> el propio termux-camera-info lo dispara
    Devuelve True si el permiso está concedido (o no aplica).
    """
    if not is_android():
        return True

    # 1) python-for-android / Kivy
    try:
        from android.permissions import Permission, check_permission, request_permissions  # type: ignore

        if check_permission(Permission.CAMERA):
            LOG.info("Permiso CAMERA ya concedido")
            return True
        LOG.info("Solicitando permiso CAMERA en runtime (API 23+)...")
        granted: dict[str, bool] = {}

        def _cb(perms: list[str], results: list[bool]) -> None:
            granted["ok"] = bool(results) and all(results)

        request_permissions([Permission.CAMERA], _cb)
        deadline = time.monotonic() + timeout
        while "ok" not in granted and time.monotonic() < deadline:
            time.sleep(0.2)
        if granted.get("ok"):
            LOG.info("Permiso CAMERA concedido por el usuario")
            return True
        if "ok" in granted:
            raise CameraPermissionError(
                "El usuario denegó el permiso de cámara. Actívalo en "
                "Ajustes > Aplicaciones > (esta app) > Permisos > Cámara."
            )
        LOG.warning("Tiempo de espera agotado esperando la respuesta al permiso")
        return False
    except ImportError:
        LOG.debug("android.permissions no disponible (no es un build de p4a)")

    # 2) pyjnius / Chaquopy
    try:
        from jnius import autoclass, cast  # type: ignore

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        Compat = autoclass("androidx.core.content.ContextCompat")
        if Compat.checkSelfPermission(activity, "android.permission.CAMERA") == 0:
            return True
        ActivityCompat = autoclass("androidx.core.app.ActivityCompat")
        ActivityCompat.requestPermissions(cast("android.app.Activity", activity),
                                          ["android.permission.CAMERA"], 1001)
        LOG.info("Solicitud de permiso CAMERA enviada vía ActivityCompat")
        return False
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Vía pyjnius no disponible: %s", exc)

    # 3) Termux:API — el primer uso abre el diálogo del sistema.
    if is_termux():
        LOG.info("Entorno Termux: el permiso de cámara lo concede el diálogo de Termux:API")
        return True

    LOG.warning("No se pudo verificar el permiso de cámara; se intentará abrir de todos modos")
    return True


def _android_camera_inventory() -> list[CameraInfo]:
    """Inventario de cámaras vía termux-camera-info (JSON de Camera2)."""
    if not shutil.which("termux-camera-info"):
        LOG.debug("termux-camera-info no disponible")
        return []
    out = _run(["termux-camera-info"], timeout=15.0)
    if not out.strip():
        LOG.warning("termux-camera-info no devolvió datos: ¿Termux:API instalado y permiso concedido?")
        return []
    try:
        entries = json.loads(out)
    except json.JSONDecodeError as exc:
        LOG.warning("Respuesta de termux-camera-info ilegible: %s", exc)
        return []

    cams: list[CameraInfo] = []
    for entry in entries if isinstance(entries, list) else []:
        cam_id = str(entry.get("id", len(cams)))
        facing = str(entry.get("facing", "unknown")).lower()
        if facing not in ("back", "front", "external"):
            facing = "unknown"
        sizes = entry.get("jpeg_output_sizes") or entry.get("output_sizes") or []
        width = height = 0
        if isinstance(sizes, list) and sizes:
            try:  # la mayor resolución disponible
                best = max(sizes, key=lambda s: int(s.get("width", 0)) * int(s.get("height", 0)))
                width, height = int(best.get("width", 0)), int(best.get("height", 0))
            except (ValueError, AttributeError, TypeError):
                pass
        try:
            index = int(cam_id)
        except ValueError:
            index = len(cams)
        label = {"back": "Cámara trasera", "front": "Cámara frontal",
                 "external": "Cámara externa"}.get(facing, "Cámara")
        cams.append(CameraInfo(
            index=index, name=f"{label} (Android id {cam_id})", device_id=f"android:{cam_id}",
            facing=facing, width=width, height=height,
            detail="Camera2/Termux:API",
        ))
    LOG.info("Android: %d cámara(s) reportadas por Camera2 (%s)",
             len(cams), ", ".join(f"{c.index}:{c.facing}" for c in cams) or "-")
    return cams


def select_android_camera(max_probe: int = 4) -> CameraInfo:
    """Elige automáticamente la cámara trasera; aplica fallbacks si no existe.

    Orden: trasera -> frontal -> externa -> cualquiera verificable -> sondeo de
    índices 0..max_probe-1. No requiere intervención del usuario.
    """
    request_android_camera_permission()

    override = os.environ.get("ALPR_ANDROID_CAMERA_ID", "").strip()
    inventory = _android_camera_inventory()

    if override:
        LOG.info("ALPR_ANDROID_CAMERA_ID=%s fuerza la cámara", override)
        try:
            forced = CameraInfo(index=int(override), name=f"Cámara Android {override}",
                                device_id=f"android:{override}", facing="unknown",
                                detail="forzada por entorno")
            if probe_camera(forced, "android"):
                return forced
            LOG.warning("La cámara forzada %s no entrega frames; sigo con la detección normal", override)
        except ValueError:
            LOG.warning("ALPR_ANDROID_CAMERA_ID no es un entero: %r", override)

    by_facing: dict[str, list[CameraInfo]] = {"back": [], "front": [], "external": [], "unknown": []}
    for cam in inventory:
        by_facing[cam.facing].append(cam)

    if not by_facing["back"]:
        LOG.warning("El dispositivo no reporta cámara trasera (tablet o equipo atípico): aplico fallback")

    # Preferencia por orientación y, dentro de cada grupo, por id más bajo
    # (en Camera2 el id 0 es la principal).
    ordered: list[CameraInfo] = []
    for facing in ("back", "front", "external", "unknown"):
        ordered += sorted(by_facing[facing], key=lambda c: (c.index if c.index is not None else 99))

    for cam in ordered:
        LOG.info("Probando %s", cam.describe())
        if probe_camera(cam, "android"):
            LOG.info("Cámara Android seleccionada: %s", cam.describe())
            return cam
        LOG.warning("Cámara Android id=%s no utilizable; siguiente candidata", cam.index)

    LOG.warning("Sin candidatas del inventario; sondeo directo de índices 0-%d", max_probe - 1)
    for idx in range(max_probe):
        cam = CameraInfo(index=idx, name=f"Cámara Android {idx}", device_id=f"android:{idx}",
                         facing="back" if idx == 0 else "unknown", detail="sondeo directo")
        if probe_camera(cam, "android"):
            LOG.info("Cámara Android seleccionada por sondeo: %s", cam.describe())
            return cam

    raise CameraUnavailableError(
        "Ninguna cámara utilizable en este dispositivo Android. Comprueba que:\n"
        "  - el permiso de Cámara esté concedido (Ajustes > Apps > Permisos)\n"
        "  - ninguna otra aplicación esté usando la cámara\n"
        "  - en Termux, que Termux:API esté instalado (pkg install termux-api)\n"
        "  - o define ALPR_ANDROID_CAMERA_ID / usa una URL RTSP como fuente"
    )


# --------------------------------------------------------------------------- #
# Cámaras IP en la red local
# --------------------------------------------------------------------------- #


def _local_subnets() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    try:  # IP local por la ruta por defecto (no envía tráfico)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))  # type: ignore[arg-type]
    except OSError as exc:
        LOG.debug("No se pudo determinar la IP local: %s", exc)
    return nets


def _check_port(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except OSError:
        return False


def discover_ip_cameras(
    subnets: Iterable[str] | None = None,
    ports: Sequence[int] = IP_CAMERA_PORTS,
    timeout: float = 0.35,
    workers: int = 128,
) -> list[CameraInfo]:
    """Descubre cámaras IP en la LAN por puertos RTSP/HTTP abiertos.

    Es un sondeo de puertos TCP en la propia subred (no una verificación de
    stream): la URL resultante es una sugerencia que el usuario debe completar
    con la ruta y credenciales de su fabricante.
    """
    networks: list[ipaddress.IPv4Network] = []
    if subnets:
        for raw in subnets:
            try:
                networks.append(ipaddress.ip_network(raw, strict=False))  # type: ignore[arg-type]
            except ValueError as exc:
                LOG.warning("Subred inválida %r: %s", raw, exc)
    else:
        networks = _local_subnets()

    if not networks:
        LOG.warning("No hay subred que explorar; omito la búsqueda de cámaras IP")
        return []

    hosts: list[str] = []
    for net in networks:
        if net.num_addresses > 1024:
            LOG.warning("Subred %s demasiado grande (%d hosts); la omito", net, net.num_addresses)
            continue
        hosts += [str(h) for h in net.hosts()]

    LOG.info("Explorando %d host(s) en %s, puertos %s",
             len(hosts), ", ".join(str(n) for n in networks), list(ports))
    found: dict[str, list[int]] = {}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        tasks = {pool.submit(_check_port, h, p, timeout): (h, p) for h in hosts for p in ports}
        for fut, (host, port) in tasks.items():
            try:
                if fut.result():
                    found.setdefault(host, []).append(port)
            except Exception as exc:  # noqa: BLE001
                LOG.debug("Error sondeando %s:%d — %s", host, port, exc)
    LOG.info("Sondeo terminado en %.1fs: %d host(s) con puertos de cámara", time.perf_counter() - t0, len(found))

    cams: list[CameraInfo] = []
    for host, open_ports in sorted(found.items()):
        rtsp_port = next((p for p in (554, 8554) if p in open_ports), None)
        try:
            hostname = socket.gethostbyaddr(host)[0]
        except OSError:
            hostname = host
        if rtsp_port:
            url = f"rtsp://{host}:{rtsp_port}{COMMON_RTSP_PATHS[0]}"
            detail = "RTSP detectado; ajusta ruta y credenciales"
        else:
            url = f"http://{host}:{open_ports[0]}/video"
            detail = "solo HTTP; verifica la ruta MJPEG del fabricante"
        cams.append(CameraInfo(
            name=f"Cámara IP {hostname}", kind="ip", url=url, device_id=host,
            facing="external", detail=f"puertos {open_ports} — {detail}",
        ))
    return cams


# --------------------------------------------------------------------------- #
# Enumeración unificada
# --------------------------------------------------------------------------- #


def enumerate_cameras(
    plat: str | None = None,
    probe: bool = True,
    include_ip: bool = False,
    ip_subnets: Iterable[str] | None = None,
    max_probe: int = 10,
) -> list[CameraInfo]:
    """Enumera cámaras de la plataforma actual, con logs detallados de cada paso."""
    plat = plat or detect_platform()
    LOG.info("Detectando cámaras en plataforma '%s' (%s %s, Python %s)",
             plat, platform.system(), platform.release(), platform.python_version())

    if plat == "android":
        cams = _android_camera_inventory() or [
            CameraInfo(index=i, name=f"Cámara Android {i}", device_id=f"android:{i}",
                       facing="back" if i == 0 else "unknown", detail="sondeo directo")
            for i in range(4)
        ]
    elif plat == "windows":
        cams = _windows_cameras()
    elif plat == "macos":
        cams = _macos_cameras()
    elif plat == "linux":
        cams = _linux_cameras()
    else:
        LOG.warning("Plataforma no reconocida; sondeo genérico de índices 0-3")
        cams = [CameraInfo(index=i, name=f"Cámara {i}") for i in range(4)]

    LOG.info("Inventario previo: %d dispositivo(s)", len(cams))
    for cam in cams:
        LOG.debug("  candidato: %s", cam.describe())

    if probe and cams:
        usable: list[CameraInfo] = []
        for cam in cams[:max_probe]:
            if probe_camera(cam, plat):
                usable.append(cam)
            else:
                LOG.warning("Descartada (no entrega frames): %s", cam.name)
        if not usable:
            LOG.warning("Ningún dispositivo del inventario respondió; conservo la lista sin verificar")
            usable = cams[:max_probe]
        cams = usable

    if include_ip:
        try:
            cams += discover_ip_cameras(ip_subnets)
        except Exception as exc:  # noqa: BLE001
            LOG.error("Fallo en la búsqueda de cámaras IP: %s", exc)

    LOG.info("Cámaras disponibles: %d", len(cams))
    for i, cam in enumerate(cams, 1):
        LOG.info("  [%d] %s", i, cam.describe())
    return cams


# --------------------------------------------------------------------------- #
# Validación de archivos de video
# --------------------------------------------------------------------------- #


def validate_video_file(path: str | Path) -> Path:
    """Comprueba existencia, permisos, extensión y decodificabilidad."""
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError as exc:
        raise VideoFileError(f"Ruta inválida {path!r}: {exc}") from exc

    if not p.exists():
        raise VideoFileError(
            f"No existe el archivo: {p}\n"
            "Si está en un pendrive o disco externo, comprueba que siga montado."
        )
    if p.is_dir():
        raise VideoFileError(f"{p} es un directorio, no un archivo de video")
    if not os.access(p, os.R_OK):
        raise VideoFileError(f"Sin permiso de lectura sobre {p}")
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise StorageDisconnectedError(f"No se puede consultar {p}: ¿medio desconectado? ({exc})") from exc
    if size == 0:
        raise VideoFileError(f"El archivo está vacío: {p}")

    if p.suffix.lower() not in VIDEO_EXTENSIONS:
        LOG.warning("Extensión inusual %r; intento abrirlo igualmente", p.suffix)

    cap = cv2.VideoCapture(str(p))
    try:
        if not cap.isOpened():
            raise VideoFileError(
                f"OpenCV no puede abrir {p.name}: formato o codec no soportado.\n"
                "Conviértelo con:  ffmpeg -i entrada -c:v libx264 -pix_fmt yuv420p salida.mp4"
            )
        ok, frame = cap.read()
        if not ok or frame is None:
            raise VideoFileError(f"{p.name} se abre pero no entrega frames decodificables (archivo corrupto?)")
        h, w = frame.shape[:2]
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = frames / fps if fps > 0 else 0.0
        LOG.info("Archivo válido: %s | %dx%d | %.2f fps | %d frames | %.1fs | %.1f MB",
                 p.name, w, h, fps, frames, dur, size / 1e6)
    finally:
        cap.release()
    return p


def list_removable_media() -> list[Path]:
    """Puntos de montaje habituales de pendrives, discos externos y tarjetas SD."""
    roots: list[Path] = []
    plat = detect_platform()
    if plat == "windows":
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:/")
            try:
                if p.exists():
                    roots.append(p)
            except OSError:
                continue
    elif plat == "macos":
        roots += [p for p in Path("/Volumes").glob("*") if p.is_dir()]
    elif plat == "android":
        roots += [p for p in (Path("/storage/emulated/0"), Path("/sdcard")) if p.exists()]
        roots += [p for p in Path("/storage").glob("*") if p.is_dir() and p.name not in ("emulated", "self")]
        dl = Path.home() / "storage" / "shared"   # Termux tras termux-setup-storage
        if dl.exists():
            roots.append(dl)
    else:  # Linux
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        for base in (Path(f"/media/{user}"), Path("/media"), Path("/run/media"), Path("/mnt")):
            if not base.exists():
                continue
            for child in base.glob("*"):
                if child.is_dir():
                    if child.name == user and (Path(f"/run/media/{user}")).exists():
                        roots += [g for g in child.glob("*") if g.is_dir()]
                    else:
                        roots.append(child)
    unique = sorted({p for p in roots})
    LOG.debug("Medios detectados: %s", [str(p) for p in unique] or "ninguno")
    return unique


# --------------------------------------------------------------------------- #
# Vista previa
# --------------------------------------------------------------------------- #


def preview_camera(cam: CameraInfo, seconds: float = 8.0, snapshot: Path | None = None) -> bool:
    """Vista previa de la cámara. Con GUI abre una ventana (q/ESC cierra);
    sin GUI mide fps reales y guarda una captura para inspección manual."""
    LOG.info("Vista previa de %s", cam.describe())
    backend = cam.backend
    cap = cv2.VideoCapture(cam.cv_source) if backend is None else cv2.VideoCapture(cam.cv_source, backend)
    if not cap.isOpened():
        LOG.error("No se pudo abrir la cámara para la vista previa")
        return False

    gui = has_gui()
    win = "Vista previa — q o ESC para cerrar"
    frames = 0
    ok_any = False
    last = None
    t0 = time.perf_counter()
    try:
        if gui:
            try:
                cv2.namedWindow(win, cv2.WINDOW_NORMAL)
            except cv2.error as exc:
                LOG.warning("Sin soporte de ventanas (%s); vista previa en modo texto", exc)
                gui = False
        while time.perf_counter() - t0 < seconds:
            ok, frame = cap.read()
            if not ok or frame is None:
                LOG.warning("Frame perdido durante la vista previa")
                time.sleep(0.05)
                continue
            ok_any = True
            frames += 1
            if gui:
                el = max(time.perf_counter() - t0, 1e-6)
                cv2.putText(frame, f"{cam.name} | {frame.shape[1]}x{frame.shape[0]} | {frames/el:.1f} fps",
                            (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2, cv2.LINE_AA)
                cv2.imshow(win, frame)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
            last = frame
        if ok_any and not gui and last is not None:
            out = snapshot or Path("preview_snapshot.jpg")
            try:
                if cv2.imwrite(str(out), last):
                    LOG.info("Captura de vista previa guardada en %s", out.resolve())
            except Exception as exc:  # noqa: BLE001
                LOG.warning("No se pudo guardar la captura: %s", exc)
    except KeyboardInterrupt:
        LOG.info("Vista previa interrumpida")
    finally:
        cap.release()
        if gui:
            try:
                cv2.destroyWindow(win)
            except cv2.error:
                pass

    el = max(time.perf_counter() - t0, 1e-6)
    if ok_any:
        LOG.info("Vista previa: %d frames en %.1fs (%.1f fps reales)", frames, el, frames / el)
    else:
        LOG.error("La cámara no entregó ningún frame en la vista previa")
    return ok_any


# --------------------------------------------------------------------------- #
# Selección interactiva (PC)
# --------------------------------------------------------------------------- #


def _ask(prompt: str, default: str = "") -> str:
    try:
        raw = input(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise SelectionAborted("Selección cancelada por el usuario") from exc
    return raw or default


def _pick_file_gui() -> Path | None:
    """Explorador gráfico (tkinter). Devuelve None si no hay tkinter/entorno."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        LOG.debug("tkinter no disponible; uso entrada de texto")
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        root.update()
        media = list_removable_media()
        initial = str(media[0]) if media else str(Path.home())
        if media:
            LOG.info("Medios extraíbles detectados: %s", ", ".join(str(m) for m in media))
        patterns = " ".join(f"*{e}" for e in VIDEO_EXTENSIONS)
        path = filedialog.askopenfilename(
            title="Selecciona un archivo de video",
            initialdir=initial,
            filetypes=[("Videos", patterns), ("Todos los archivos", "*.*")],
        )
        root.destroy()
        return Path(path) if path else None
    except Exception as exc:  # noqa: BLE001
        LOG.warning("El explorador gráfico falló (%s); uso entrada de texto", exc)
        return None


def _pick_file_text() -> Path:
    """Selección por texto. Devuelve una ruta ya validada."""
    media = list_removable_media()
    if media:
        print("\nMedios de almacenamiento detectados:")
        for i, m in enumerate(media, 1):
            print(f"  [{i}] {m}")
        print("  Escribe el número para explorar ese medio, o una ruta completa.")
    while True:
        raw = _ask("\nRuta del archivo de video (o 'q' para cancelar): ")
        if raw.lower() in ("q", "salir", "quit"):
            raise SelectionAborted("Selección cancelada")
        if raw.isdigit() and media and 1 <= int(raw) <= len(media):
            base = media[int(raw) - 1]
            vids = sorted(
                (p for p in base.rglob("*") if p.suffix.lower() in VIDEO_EXTENSIONS and p.is_file()),
                key=lambda p: p.name,
            )[:50]
            if not vids:
                print(f"  No se encontraron videos en {base}")
                continue
            print(f"\nVideos en {base}:")
            for i, v in enumerate(vids, 1):
                try:
                    mb = v.stat().st_size / 1e6
                except OSError:
                    mb = 0.0
                print(f"  [{i}] {v.relative_to(base)}  ({mb:.1f} MB)")
            sel = _ask("Número del video: ")
            if sel.isdigit() and 1 <= int(sel) <= len(vids):
                return vids[int(sel) - 1]
            print("  Selección inválida.")
            continue
        try:
            return validate_video_file(raw)
        except SourceError as exc:
            print(f"  {exc}")


def choose_file_source() -> VideoSourceSpec:
    """Opción 1 del menú: procesar video desde archivo."""
    path: Path | None = None
    already_validated = False
    if has_gui():
        path = _pick_file_gui()
        if path is None:
            LOG.info("Sin selección en el explorador; paso a entrada por texto")
    if path is None:
        path = _pick_file_text()
        already_validated = True
    valid = path if already_validated else validate_video_file(path)
    return VideoSourceSpec(cv_source=str(valid), kind="file", label=valid.name, is_live=False, path=valid)


def choose_camera_source(include_ip: bool = False, ip_subnets: Iterable[str] | None = None,
                         allow_preview: bool = True) -> VideoSourceSpec:
    """Opción 2 del menú: usar cámara en vivo (local, USB o IP)."""
    cams = enumerate_cameras(include_ip=include_ip, ip_subnets=ip_subnets)
    if not cams and not include_ip:
        print("\nNo se detectaron cámaras locales.")
        if _ask("¿Buscar cámaras IP en la red local? [s/N]: ", "n").lower().startswith("s"):
            cams = discover_ip_cameras(ip_subnets)
    if not cams:
        raise CameraUnavailableError(
            "No se detectó ninguna cámara. Comprueba que esté conectada, que no la use "
            "otra aplicación y que tengas permisos (en Linux, grupo 'video'; en Windows y "
            "macOS, Configuración > Privacidad > Cámara)."
        )

    while True:
        print("\nCámaras detectadas:")
        for i, cam in enumerate(cams, 1):
            print(f"  [{i}] {cam.describe()}")
        print("  [r] Introducir una URL RTSP/HTTP manualmente")
        if include_ip is False:
            print("  [b] Buscar cámaras IP en la red local")
        if allow_preview:
            print("  [p] Vista previa de una cámara")
        print("  [q] Cancelar")

        sel = _ask("\nSelecciona la cámara: ")
        low = sel.lower()
        if low in ("q", "salir"):
            raise SelectionAborted("Selección cancelada")
        if low == "r":
            url = _ask("URL (p. ej. rtsp://usuario:clave@192.168.1.50:554/stream1): ")
            if not url:
                continue
            cam = CameraInfo(name="Cámara por URL", kind="ip", url=url, device_id=url, facing="external")
            if not probe_camera(cam):
                print("  Advertencia: no se pudo verificar el stream. Se usará de todos modos.")
            return VideoSourceSpec(cv_source=url, kind="ip", label=url, is_live=True, camera=cam)
        if low == "b":
            cams += discover_ip_cameras(ip_subnets)
            include_ip = True
            continue
        if low == "p" and allow_preview:
            which = _ask("Número de la cámara a previsualizar: ")
            if which.isdigit() and 1 <= int(which) <= len(cams):
                preview_camera(cams[int(which) - 1])
            continue
        if sel.isdigit() and 1 <= int(sel) <= len(cams):
            cam = cams[int(sel) - 1]
            if allow_preview and _ask(f"¿Vista previa de «{cam.name}» antes de continuar? [s/N]: ", "n") \
                    .lower().startswith("s"):
                if not preview_camera(cam):
                    print("  La cámara no entregó imagen. Elige otra.")
                    continue
            if not cam.verified and not probe_camera(cam):
                print("  Esa cámara no entrega frames. Elige otra.")
                continue
            return VideoSourceSpec(
                cv_source=cam.cv_source, kind="ip" if cam.kind == "ip" else "camera",
                label=cam.name, is_live=True, backend=cam.backend, camera=cam,
            )
        print("  Opción inválida.")


def select_source(
    include_ip: bool = False,
    ip_subnets: Iterable[str] | None = None,
    allow_preview: bool = True,
    force_platform: str | None = None,
) -> VideoSourceSpec:
    """Punto de entrada: automático en Android, menú de dos opciones en PC."""
    plat = force_platform or detect_platform()
    LOG.info("Plataforma detectada: %s | GUI=%s | consola interactiva=%s",
             plat, has_gui(), is_interactive())

    if plat == "android":
        cam = select_android_camera()
        return VideoSourceSpec(
            cv_source=cam.cv_source, kind="camera", label=cam.name, is_live=True,
            backend=cam.backend, camera=cam, platform_name=plat,
        )

    if not is_interactive():
        raise SelectionAborted(
            "No hay consola interactiva para elegir la fuente. Indica -i/--input "
            "o define ALPR_INPUT (así trabaja el servicio systemd)."
        )

    while True:
        print("\n" + "=" * 58)
        print("  placa-recon — selección de fuente de video")
        print("=" * 58)
        print("  [1] Procesar video desde archivo")
        print("      (disco local, pendrive, disco externo, tarjeta SD)")
        print("  [2] Usar cámara en vivo")
        print("      (webcam integrada, cámara USB, cámara IP de la red)")
        print("  [q] Salir")
        opt = _ask("\nOpción [1/2]: ")
        try:
            if opt == "1":
                return choose_file_source()
            if opt == "2":
                return choose_camera_source(include_ip=include_ip, ip_subnets=ip_subnets,
                                            allow_preview=allow_preview)
            if opt.lower() in ("q", "salir"):
                raise SelectionAborted("Selección cancelada")
            print("  Opción inválida.")
        except SelectionAborted:
            raise
        except SourceError as exc:
            print(f"\n  {exc}\n")
            if not _ask("¿Volver al menú? [S/n]: ", "s").lower().startswith("s"):
                raise


# --------------------------------------------------------------------------- #
# CLI de diagnóstico
# --------------------------------------------------------------------------- #


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnóstico de fuentes de video de placa-recon")
    ap.add_argument("--list", action="store_true", help="Enumerar cámaras y salir")
    ap.add_argument("--select", action="store_true", help="Menú interactivo de selección")
    ap.add_argument("--scan-ip", action="store_true", help="Incluir cámaras IP de la red local")
    ap.add_argument("--subnet", action="append", help="Subred a explorar (p. ej. 192.168.1.0/24)")
    ap.add_argument("--preview", type=int, metavar="N", help="Vista previa de la cámara número N")
    ap.add_argument("--media", action="store_true", help="Listar medios de almacenamiento detectados")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    print(f"Plataforma: {detect_platform()} | Android={is_android()} | Termux={is_termux()} "
          f"| GUI={has_gui()} | OpenCV={cv2.__version__}")

    if args.media:
        for m in list_removable_media():
            print(f"  {m}")
        return 0
    if args.preview is not None:
        cams = enumerate_cameras(include_ip=args.scan_ip, ip_subnets=args.subnet)
        if not 1 <= args.preview <= len(cams):
            print(f"Índice fuera de rango (1-{len(cams)})")
            return 1
        return 0 if preview_camera(cams[args.preview - 1]) else 1
    if args.select:
        try:
            spec = select_source(include_ip=args.scan_ip, ip_subnets=args.subnet)
        except SourceError as exc:
            print(f"\n{exc}")
            return 1
        print(f"\nFuente elegida: {spec.label}\n  -i {spec.as_input_string()}  (vivo={spec.is_live})")
        return 0

    enumerate_cameras(include_ip=args.scan_ip, ip_subnets=args.subnet)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
