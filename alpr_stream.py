#!/usr/bin/env python3
"""
ALPR sobre video y cámaras RTSP en tiempo real, con fast-alpr + OpenCV.

- Procesa archivos de video o streams en vivo (RTSP/HTTP/webcam) cuadro por cuadro.
- Registra en CSV las placas con confianza > umbral (por defecto 0.8).
- Evita duplicados de la misma placa dentro de una ventana de 5 s.
- Genera video anotado con bounding box y texto superpuesto (con segmentación
  opcional por minutos en modo vivo).
- Guarda recortes JPEG individuales de cada placa (y opcionalmente el frame
  completo), organizados por fecha.
- Toda opción admite variable de entorno ALPR_* (ver alpr.env.example / systemd).
- Modo vivo: lectura en hilo aparte, descarte de frames obsoletos y reconexión
  automática con backoff exponencial.
- Fuente multiplataforma (video_source.py): en Android usa automáticamente la
  cámara trasera; en PC ofrece un menú para elegir archivo o cámara en vivo.

Uso:
  # Archivo
  python alpr_stream.py -i entrada.mp4 -o salida.mp4 -c placas.csv

  # Cámara RTSP en tiempo real (24/7, segmentos de 10 min)
  python alpr_stream.py -i rtsp://user:pass@192.168.1.50:554/stream1 \
      -o grabacion.mp4 -c placas.csv --segment-minutes 10 --hud

  # Webcam local, solo CSV, 2 h de captura
  python alpr_stream.py -i 0 --no-video --duration 7200
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

import re

import cv2

# Permite importar video_source.py aunque el cwd sea otro (p. ej. bajo systemd).
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from video_source import (
        CameraInfo,
        SelectionAborted,
        SourceError,
        StorageDisconnectedError,
        VideoFileError,
        VideoSourceSpec,
        detect_platform,
        enumerate_cameras,
        preview_camera,
        select_source,
        validate_video_file,
    )
    HAS_SOURCE_MODULE = True
except ImportError as _exc:  # el script sigue siendo usable sin el módulo
    HAS_SOURCE_MODULE = False
    _SOURCE_IMPORT_ERROR = _exc

LOG = logging.getLogger("alpr")

LIVE_SCHEMES = ("rtsp", "rtsps", "rtmp", "http", "https", "udp", "tcp", "srt", "mms")


# --------------------------------------------------------------------------- #
# Utilidades de extracción tolerante (la forma exacta del resultado de
# fast-alpr puede variar entre versiones: objetos, dicts o tuplas).
# --------------------------------------------------------------------------- #


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class PlateHit:
    text: str
    ocr_conf: float
    det_conf: float
    box: tuple[int, int, int, int] | None  # x1, y1, x2, y2

    @property
    def score(self) -> float:
        """Confianza efectiva: la más restrictiva disponible."""
        confs = [c for c in (self.ocr_conf, self.det_conf) if c > 0.0]
        return min(confs) if confs else 0.0


def parse_result(result: Any) -> PlateHit | None:
    """Normaliza un resultado de ALPR.predict() a PlateHit."""
    ocr = _get(result, "ocr", "ocr_result", "recognition")
    text = _get(ocr, "text", "plate", "label") or _get(result, "text", "plate")
    if not text:
        return None
    text = str(text).strip().upper().replace(" ", "")
    if not text:
        return None

    ocr_conf = _as_float(_get(ocr, "confidence", "conf", "score"))

    det = _get(result, "detection", "det", "bbox_result")
    det_conf = _as_float(_get(det, "confidence", "conf", "score"))

    bbox = _get(det, "bounding_box", "bbox", "box") or _get(result, "bounding_box", "bbox")
    box: tuple[int, int, int, int] | None = None
    if bbox is not None:
        x1 = _get(bbox, "x1", "xmin", "left")
        y1 = _get(bbox, "y1", "ymin", "top")
        x2 = _get(bbox, "x2", "xmax", "right")
        y2 = _get(bbox, "y2", "ymax", "bottom")
        if None in (x1, y1, x2, y2) and isinstance(bbox, Iterable):
            try:
                x1, y1, x2, y2 = list(bbox)[:4]
            except ValueError:
                x1 = None
        if None not in (x1, y1, x2, y2):
            box = (int(x1), int(y1), int(x2), int(y2))

    return PlateHit(text=text, ocr_conf=ocr_conf, det_conf=det_conf, box=box)


# --------------------------------------------------------------------------- #
# Deduplicación temporal
# --------------------------------------------------------------------------- #


class DedupWindow:
    """Suprime repeticiones de la misma placa dentro de `window` segundos,
    con purga periódica del caché."""

    def __init__(self, window: float = 5.0) -> None:
        self.window = window
        self._last: dict[str, float] = {}

    def accept(self, plate: str, t_stream: float) -> bool:
        prev = self._last.get(plate)
        if prev is not None and (t_stream - prev) < self.window:
            return False
        self._last[plate] = t_stream
        if len(self._last) > 4096:
            cutoff = t_stream - self.window
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}
        return True


# --------------------------------------------------------------------------- #
# Fuentes de video
# --------------------------------------------------------------------------- #


def redact(source: str) -> str:
    """Oculta credenciales de una URL RTSP para logs seguros."""
    try:
        u = urlparse(source)
    except ValueError:
        return source
    if not u.hostname or "@" not in (u.netloc or ""):
        return source
    host = u.hostname + (f":{u.port}" if u.port else "")
    return urlunparse(u._replace(netloc=f"{u.username or 'user'}:***@{host}"))


def is_live_source(source: str) -> bool:
    if source.isdigit():
        return True
    scheme = urlparse(source).scheme.lower()
    return scheme in LIVE_SCHEMES


def _cap_source(source: str) -> Any:
    return int(source) if source.isdigit() else source


def open_capture(source: str, buffer_size: int = 1, backend: int | None = None) -> cv2.VideoCapture:
    src = _cap_source(source)
    cap = cv2.VideoCapture(src) if backend is None else cv2.VideoCapture(src, backend)
    if cap.isOpened():
        try:  # No todos los backends lo soportan; reduce latencia en RTSP.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)
        except Exception:  # noqa: BLE001
            pass
    return cap


class LiveReader:
    """Lector RTSP/webcam en hilo propio.

    Mantiene solo el frame más reciente (los intermedios se descartan) para que
    la inferencia trabaje siempre sobre tiempo real en lugar de acumular retardo
    en el búfer del socket. Reconecta con backoff exponencial si el stream cae.
    """

    def __init__(
        self,
        source: str,
        buffer_size: int = 1,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
        read_timeout: float = 15.0,
        backend: int | None = None,
    ) -> None:
        self.source = source
        self.backend = backend
        self.buffer_size = buffer_size
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.read_timeout = read_timeout

        self._lock = threading.Lock()
        self._frame = None
        self._seq = 0            # nº de frame recibido de la cámara
        self._last_recv = 0.0
        self._new = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.dropped = 0         # frames recibidos que nunca se procesaron
        self.reconnects = 0
        self.connected = False
        self.meta: dict[str, float] = {}

    # -- ciclo de vida ----------------------------------------------------- #

    def start(self) -> "LiveReader":
        cap = self._connect(initial=True)
        if cap is None:
            raise SystemExit(f"No se pudo abrir la fuente en vivo: {redact(self.source)}")
        self._thread = threading.Thread(target=self._loop, args=(cap,), daemon=True, name="live-reader")
        self._thread.start()
        return self

    def _connect(self, initial: bool = False) -> cv2.VideoCapture | None:
        delay = self.reconnect_delay
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            cap = open_capture(self.source, self.buffer_size, self.backend)
            if cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    self.connected = True
                    self.meta = {
                        "fps": cap.get(cv2.CAP_PROP_FPS) or 0.0,
                        "width": cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0,
                        "height": cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0,
                    }
                    self._publish(frame)
                    LOG.info("Conectado a %s (intento %d)", redact(self.source), attempt)
                    return cap
            cap.release()
            if initial and attempt == 1:
                LOG.warning("Conexión fallida con %s; reintentando...", redact(self.source))
            LOG.warning("Reintento en %.1fs (intento %d)", delay, attempt)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, self.max_reconnect_delay)
        return None

    def _publish(self, frame) -> None:
        with self._lock:
            if self._frame is not None and not self._new.is_set():
                pass
            if self._new.is_set():
                self.dropped += 1  # el consumidor no alcanzó a leer el anterior
            self._frame = frame
            self._seq += 1
            self._last_recv = time.monotonic()
        self._new.set()

    def _loop(self, cap: cv2.VideoCapture) -> None:
        fail = 0
        while not self._stop.is_set():
            try:
                ok, frame = cap.read()
            except Exception as exc:  # noqa: BLE001
                LOG.error("Error leyendo del stream: %s", exc)
                ok, frame = False, None
            if ok and frame is not None:
                fail = 0
                self._publish(frame)
                continue

            fail += 1
            self.connected = False
            if fail < 5:
                time.sleep(0.05)
                continue
            LOG.warning("Stream caído (%s); reconectando...", redact(self.source))
            cap.release()
            new_cap = self._connect()
            if new_cap is None:
                break
            cap = new_cap
            self.reconnects += 1
            fail = 0
        cap.release()
        self.connected = False
        self._new.set()  # despierta al consumidor

    def read(self, timeout: float = 1.0):
        """Devuelve (ok, frame, seq) con el frame más reciente disponible."""
        if not self._new.wait(timeout):
            if self._last_recv and (time.monotonic() - self._last_recv) > self.read_timeout:
                LOG.error("Sin frames por más de %.0fs", self.read_timeout)
            return False, None, self._seq
        with self._lock:
            frame, seq = self._frame, self._seq
            self._new.clear()
        if frame is None:
            return False, None, seq
        return True, frame, seq

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        self._new.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# Escritor CSV incremental (flush por fila: seguro ante cortes)
# --------------------------------------------------------------------------- #

CSV_FIELDS = [
    "camera_id",
    "plate",
    "frame_id",
    "crop_path",
    "frame_path",
    "stream_timestamp_s",
    "stream_timestamp_hms",
    "wallclock_local",
    "wallclock_utc",
    "source",
    "ocr_confidence",
    "detection_confidence",
    "x1",
    "y1",
    "x2",
    "y2",
]


class CsvLogger:
    def __init__(self, path: Path, source: str, camera_id: str = "") -> None:
        self.path = path
        self.source = source
        # Identificador lógico de la cámara: lo inyecta el panel web (ALPR_CAMERA_ID)
        # para poder agrupar detecciones aunque cambie la URL de la fuente.
        self.camera_id = camera_id or os.environ.get("ALPR_CAMERA_ID", "")
        new = not path.exists() or path.stat().st_size == 0
        # Compatibilidad con CSV creados por versiones anteriores: si el archivo ya
        # tiene encabezado, se respeta el suyo para no descolocar las columnas.
        campos = CSV_FIELDS
        if not new:
            try:
                with path.open("r", newline="", encoding="utf-8") as fh:
                    cabecera = next(csv.reader(fh), [])
                if cabecera:
                    campos = [c for c in cabecera if c]
            except OSError:
                pass
        self.fields = campos
        self._fh = path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=self.fields,
                                      restval="", extrasaction="ignore")
        if new:
            self._writer.writeheader()
            self._fh.flush()
        self.rows = 0

    def write(
        self,
        hit: PlateHit,
        frame_id: int,
        t_stream: float,
        crop_path: str = "",
        frame_path: str = "",
    ) -> None:
        x1, y1, x2, y2 = hit.box if hit.box else ("", "", "", "")
        now = datetime.now().astimezone()
        self._writer.writerow(
            {
                "camera_id": self.camera_id,
                "plate": hit.text,
                "frame_id": frame_id,
                "crop_path": crop_path,
                "frame_path": frame_path,
                "stream_timestamp_s": f"{t_stream:.3f}",
                "stream_timestamp_hms": hms(t_stream),
                "wallclock_local": now.isoformat(timespec="milliseconds"),
                "wallclock_utc": now.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
                "source": self.source,
                "ocr_confidence": f"{hit.ocr_conf:.4f}",
                "detection_confidence": f"{hit.det_conf:.4f}",
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
        )
        self._fh.flush()
        self.rows += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


def hms(seconds: float) -> str:
    ms = int(round(max(seconds, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# --------------------------------------------------------------------------- #
# Guardado de recortes de placa
# --------------------------------------------------------------------------- #

_SAFE = re.compile(r"[^A-Z0-9_-]+")


def safe_name(text: str) -> str:
    cleaned = _SAFE.sub("", text.upper())
    return cleaned or "UNKNOWN"


class CropSaver:
    """Guarda una imagen JPEG por cada placa detectada.

    Estructura: <root>/<YYYY-MM-DD>/<HHMMSS-mmm>_<PLACA>_<conf>_f<frame>.jpg
    y, si se pide, el frame completo en <root>/<fecha>/frames/.
    """

    def __init__(
        self,
        root: Path,
        margin: float = 0.15,
        quality: int = 92,
        min_size: int = 16,
        save_frame: bool = False,
        by_plate: bool = False,
    ) -> None:
        self.root = root
        self.margin = max(margin, 0.0)
        self.quality = int(min(max(quality, 1), 100))
        self.min_size = min_size
        self.save_frame = save_frame
        self.by_plate = by_plate
        self.saved = 0
        self.failed = 0
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, plate: str) -> Path:
        day = datetime.now().strftime("%Y-%m-%d")
        path = self.root / (f"{day}/{safe_name(plate)}" if self.by_plate else day)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, frame, hit: PlateHit, frame_id: int) -> tuple[str, str]:
        """Devuelve (ruta_recorte, ruta_frame); cadena vacia si no se guardo."""
        if hit.box is None:
            return "", ""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = hit.box
        mx = int((x2 - x1) * self.margin)
        my = int((y2 - y1) * self.margin)
        cx1, cy1 = max(0, x1 - mx), max(0, y1 - my)
        cx2, cy2 = min(w, x2 + mx), min(h, y2 + my)
        if cx2 - cx1 < self.min_size or cy2 - cy1 < self.min_size:
            LOG.debug("Recorte descartado por tamano (%dx%d)", cx2 - cx1, cy2 - cy1)
            return "", ""

        now = datetime.now()
        stamp = now.strftime("%H%M%S-") + f"{now.microsecond // 1000:03d}"
        base = f"{stamp}_{safe_name(hit.text)}_{int(round(hit.score * 100)):03d}_f{frame_id}"
        out_dir = self._dir(hit.text)
        params = [cv2.IMWRITE_JPEG_QUALITY, self.quality]

        crop_str = frame_str = ""
        try:
            crop_path = out_dir / f"{base}.jpg"
            if cv2.imwrite(str(crop_path), frame[cy1:cy2, cx1:cx2], params):
                crop_str = str(crop_path)
                self.saved += 1
            else:
                self.failed += 1
                LOG.error("No se pudo escribir el recorte %s", crop_path)
            if self.save_frame:
                fdir = out_dir / "frames"
                fdir.mkdir(parents=True, exist_ok=True)
                fpath = fdir / f"{base}_full.jpg"
                if cv2.imwrite(str(fpath), frame, params):
                    frame_str = str(fpath)
        except Exception as exc:  # noqa: BLE001 - nunca abortar el pipeline por E/S
            self.failed += 1
            LOG.error("Error guardando imagen de %s: %s", hit.text, exc)
        return crop_str, frame_str


# --------------------------------------------------------------------------- #
# Escritura de video (con segmentación opcional)
# --------------------------------------------------------------------------- #


class SegmentedWriter:
    """VideoWriter que rota el archivo cada `segment_minutes` (0 = archivo único)."""

    def __init__(self, base: Path, fps: float, fourcc: str, segment_minutes: float = 0.0) -> None:
        self.base = base
        self.fps = max(fps, 1.0)
        self.fourcc = fourcc
        self.segment_seconds = segment_minutes * 60.0
        self._writer: cv2.VideoWriter | None = None
        self._size: tuple[int, int] | None = None
        self._opened_at = 0.0
        self.paths: list[Path] = []

    def _path(self) -> Path:
        if not self.segment_seconds:
            return self.base
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.base.with_name(f"{self.base.stem}_{stamp}{self.base.suffix}")

    def _open(self, size: tuple[int, int]) -> None:
        path = self._path()
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*self.fourcc), self.fps, size)
        if not writer.isOpened():
            raise SystemExit(
                f"No se pudo crear el video '{path}' con codec {self.fourcc}. "
                "Prueba --fourcc XVID con extensión .avi."
            )
        self._writer, self._size, self._opened_at = writer, size, time.monotonic()
        self.paths.append(path)
        LOG.info("Grabando en %s", path)

    def write(self, frame) -> None:
        h, w = frame.shape[:2]
        if self._writer is None:
            self._open((w, h))
        elif self._size != (w, h):
            frame = cv2.resize(frame, self._size)
        elif self.segment_seconds and (time.monotonic() - self._opened_at) >= self.segment_seconds:
            self.release()
            self._open((w, h))
        self._writer.write(frame)  # type: ignore[union-attr]

    def release(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None


# --------------------------------------------------------------------------- #
# Dibujado
# --------------------------------------------------------------------------- #

GREEN = (0, 200, 0)
RED = (0, 0, 220)
BLACK = (0, 0, 0)


class VistaEnVivo:
    """Publica el último fotograma anotado como JPEG para el panel web.

    Escribe siempre en un temporal y lo mueve con ``os.replace`` (atómico), de
    modo que el panel nunca lea un JPEG a medio escribir. El ritmo se limita a
    ``fps`` para no gastar CPU: la vista en vivo es de vigilancia, no de video.
    """

    def __init__(self, ruta: Path, fps: float = 3.0, ancho: int = 640,
                 calidad: int = 70) -> None:
        self.ruta = ruta
        self.tmp = ruta.with_suffix(ruta.suffix + ".tmp")
        self.periodo = 1.0 / fps if fps > 0 else 0.0
        self.ancho = max(160, ancho)
        self.calidad = max(1, min(100, calidad))
        self.publicados = 0
        self.fallidos = 0
        self._proximo = 0.0
        ruta.parent.mkdir(parents=True, exist_ok=True)

    def publicar(self, frame) -> bool:
        ahora = time.perf_counter()
        if self.periodo and ahora < self._proximo:
            return False
        self._proximo = ahora + self.periodo
        try:
            vista = frame
            h, w = frame.shape[:2]
            if w > self.ancho:
                escala = self.ancho / float(w)
                vista = cv2.resize(frame, (self.ancho, max(1, int(h * escala))),
                                   interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", vista,
                                   [cv2.IMWRITE_JPEG_QUALITY, self.calidad])
            if not ok:
                self.fallidos += 1
                return False
            self.tmp.write_bytes(buf.tobytes())
            os.replace(self.tmp, self.ruta)
        except (OSError, cv2.error) as exc:
            self.fallidos += 1
            LOG.debug("No se pudo publicar la vista en vivo: %s", exc)
            return False
        self.publicados += 1
        return True

    def cerrar(self) -> None:
        """Borra el JPEG para que el panel marque la cámara como sin señal."""
        for ruta in (self.tmp, self.ruta):
            try:
                ruta.unlink()
            except OSError:
                pass


def draw_hit(frame, hit: PlateHit) -> None:
    if not hit.box:
        return
    h, w = frame.shape[:2]
    x1 = max(0, min(hit.box[0], w - 1))
    y1 = max(0, min(hit.box[1], h - 1))
    x2 = max(0, min(hit.box[2], w - 1))
    y2 = max(0, min(hit.box[3], h - 1))
    cv2.rectangle(frame, (x1, y1), (x2, y2), GREEN, 2)

    label = f"{hit.text} {hit.score:.2f}"
    scale = max(0.5, min(1.0, (x2 - x1) / 220.0))
    (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    ty = y1 - 6 if y1 - th - base - 6 >= 0 else y2 + th + base + 6
    bg_top = ty - th - base
    cv2.rectangle(frame, (x1, max(0, bg_top)), (min(w - 1, x1 + tw + 8), min(h - 1, ty + 4)), GREEN, -1)
    cv2.putText(frame, label, (x1 + 4, ty), cv2.FONT_HERSHEY_SIMPLEX, scale, BLACK, 2, cv2.LINE_AA)


def draw_hud(frame, frame_id: int, t_stream: float, logged: int, fps_proc: float, live_info: str = "") -> None:
    txt = f"frame {frame_id} | t {hms(t_stream)} | logs {logged} | {fps_proc:.1f} fps"
    if live_info:
        txt += f" | {live_info}"
    cv2.rectangle(frame, (0, 0), (min(frame.shape[1], 760), 52), BLACK, -1)
    cv2.putText(frame, txt, (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(frame, stamp, (8, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1, cv2.LINE_AA)


def draw_offline(frame) -> None:
    cv2.putText(frame, "RECONECTANDO...", (12, frame.shape[0] - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, RED, 2, cv2.LINE_AA)


# --------------------------------------------------------------------------- #
# Núcleo
# --------------------------------------------------------------------------- #

_STOP = False


def _handle_signal(*_: Any) -> None:
    global _STOP
    _STOP = True
    LOG.warning("Señal recibida: cerrando de forma ordenada...")


def build_alpr(detector: str, ocr: str):
    try:
        from fast_alpr import ALPR
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Falta la dependencia 'fast-alpr'. Instala con:\n"
            "  pip install fast-alpr opencv-python"
        ) from exc
    LOG.info("Cargando modelos (detector=%s, ocr=%s)...", detector, ocr)
    return ALPR(detector_model=detector, ocr_model=ocr)


def configure_ffmpeg(args: argparse.Namespace) -> None:
    """Opciones del backend FFmpeg para RTSP: transporte y timeouts.

    Debe ejecutarse antes de crear cualquier VideoCapture.
    """
    if "OPENCV_FFMPEG_CAPTURE_OPTIONS" in os.environ:
        return
    micros = int(max(args.rtsp_timeout, 1.0) * 1_000_000)
    opts = [
        f"rtsp_transport;{args.rtsp_transport}",
        f"stimeout;{micros}",
        f"timeout;{micros}",
        "max_delay;500000",
        "reorder_queue_size;0",
        "buffer_size;1048576",
    ]
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)
    LOG.debug("OPENCV_FFMPEG_CAPTURE_OPTIONS=%s", os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"])


def resolve_source(args: argparse.Namespace) -> argparse.Namespace:
    """Determina la fuente de video: explícita, automática (Android) o elegida por el usuario (PC).

    Deja en `args.input` una cadena que el resto del pipeline abre igual, sea
    índice de cámara, ruta de archivo o URL de red.
    """
    args.capture_backend = None
    if args.input and not args.select_source:
        # Fuente explícita: validamos los archivos antes de cargar los modelos.
        if HAS_SOURCE_MODULE and not is_live_source(args.input) and not args.input.isdigit():
            try:
                validate_video_file(args.input)
            except SourceError as exc:
                raise SystemExit(f"Fuente inválida: {exc}")
        return args

    if not HAS_SOURCE_MODULE:
        raise SystemExit(
            f"No se pudo importar video_source.py ({_SOURCE_IMPORT_ERROR}); "
            "indica la fuente con -i/--input o ALPR_INPUT."
        )

    plat = detect_platform()
    LOG.info("Sin fuente explícita: resolviendo según plataforma (%s)", plat)
    try:
        spec: VideoSourceSpec = select_source(
            include_ip=args.scan_ip_cameras,
            ip_subnets=args.ip_subnet,
            allow_preview=not args.no_preview,
        )
    except SelectionAborted as exc:
        raise SystemExit(str(exc))
    except SourceError as exc:
        raise SystemExit(f"No se pudo determinar la fuente de video: {exc}")

    args.input = spec.as_input_string()
    args.capture_backend = spec.backend
    if spec.is_live:
        args.live = True
    else:
        args.no_live = True
    LOG.info("Fuente seleccionada [%s]: %s", spec.kind, spec.label or args.input)
    return args


def run(args: argparse.Namespace) -> int:
    args = resolve_source(args)
    live = args.live or (is_live_source(args.input) and not args.no_live)
    if live:
        configure_ffmpeg(args)

    alpr = build_alpr(args.detector_model, args.ocr_model)
    src_label = redact(args.input)

    reader: LiveReader | None = None
    cap: cv2.VideoCapture | None = None

    if live:
        reader = LiveReader(
            args.input,
            buffer_size=args.buffer_size,
            reconnect_delay=args.reconnect_delay,
            max_reconnect_delay=args.max_reconnect_delay,
            read_timeout=args.read_timeout,
            backend=getattr(args, "capture_backend", None),
        ).start()
        fps_in = reader.meta.get("fps") or 0.0
        width, height, total = int(reader.meta.get("width", 0)), int(reader.meta.get("height", 0)), 0
    else:
        cap = open_capture(args.input, args.buffer_size, getattr(args, "capture_backend", None))
        if not cap.isOpened():
            raise SystemExit(f"No se pudo abrir la fuente de video: {src_label}")
        src_file = Path(args.input) if not is_live_source(args.input) and not args.input.isdigit() else None
        fps_in = cap.get(cv2.CAP_PROP_FPS) or 0.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    if not fps_in or fps_in <= 0 or fps_in > 1000:
        fps_in = args.fallback_fps
        LOG.warning("FPS no reportado por la fuente; usando %.2f", fps_in)

    LOG.info(
        "Modo %s | %s | %dx%d @ %.2f fps | frames=%s",
        "EN VIVO" if live else "ARCHIVO", src_label, width, height, fps_in, total or "?",
    )

    csv_log = CsvLogger(Path(args.csv), src_label)
    dedup = DedupWindow(args.dedup_window)
    crops: CropSaver | None = None
    if args.save_crops:
        crops = CropSaver(
            Path(args.crops_dir),
            margin=args.crop_margin,
            quality=args.jpeg_quality,
            min_size=args.crop_min_size,
            save_frame=args.save_full_frame,
            by_plate=args.crops_by_plate,
        )
        LOG.info("Recortes en %s (margen %.0f%%, calidad %d)",
                 crops.root.resolve(), args.crop_margin * 100, crops.quality)
    writer: SegmentedWriter | None = None
    if not args.no_video:
        record_fps = args.record_fps or (args.target_fps if live and args.target_fps else fps_in)
        writer = SegmentedWriter(Path(args.output), record_fps, args.fourcc, args.segment_minutes)

    vista: VistaEnVivo | None = None
    if args.live_view:
        vista = VistaEnVivo(
            Path(args.live_view),
            fps=args.live_view_fps,
            ancho=args.live_view_width,
            calidad=args.live_view_quality,
        )
        LOG.info("Vista en vivo en %s (%.1f fps, ancho %d px)",
                 vista.ruta.resolve(), args.live_view_fps, args.live_view_width)

    # Se necesita un lienzo anotado si se graba video, se previsualiza en
    # pantalla o se publica la vista en vivo del panel.
    anotar = (not args.no_video) or args.preview or vista is not None

    frame_id = 0
    processed = 0
    detections = 0
    skipped_live = 0
    t0 = time.perf_counter()
    min_period = 1.0 / args.target_fps if (live and args.target_fps) else 0.0
    next_infer = 0.0
    last_annotated = None

    try:
        while not _STOP:
            if reader is not None:
                ok, frame, _ = reader.read(timeout=1.0)
                if not ok:
                    if not reader.alive:
                        LOG.error("El lector del stream terminó; abortando.")
                        break
                    if last_annotated is not None and (vista is not None or
                            (writer is not None and args.keep_recording_offline)):
                        offline = last_annotated.copy()
                        draw_offline(offline)
                        if writer is not None and args.keep_recording_offline:
                            writer.write(offline)
                        if vista is not None:
                            vista.publicar(offline)
                    continue
            else:
                ok, frame = cap.read()  # type: ignore[union-attr]
                if not ok:
                    # Distingue fin de archivo de una desconexión del medio
                    # (pendrive, disco externo o unidad de red retirada).
                    if src_file is not None:
                        try:
                            missing = not src_file.exists()
                        except OSError as exc:
                            LOG.error("El almacenamiento no responde (%s): %s", src_file, exc)
                            missing = True
                        if missing:
                            LOG.error(
                                "El archivo dejó de estar accesible en el frame %d: "
                                "¿se desconectó el dispositivo de almacenamiento? (%s)",
                                frame_id, src_file,
                            )
                            LOG.info("Cierro conservando el CSV y el video generados hasta ahora.")
                            break
                    LOG.info("Fin del video.")
                    break
            frame_id += 1

            if live:
                # En vivo el reloj de pared es la referencia fiable.
                t_stream = time.perf_counter() - t0
                if min_period and t_stream < next_infer:
                    skipped_live += 1
                    continue
                next_infer = t_stream + min_period
                do_infer = True
            else:
                pos_ms = cap.get(cv2.CAP_PROP_POS_MSEC)  # type: ignore[union-attr]
                t_stream = pos_ms / 1000.0 if pos_ms and pos_ms > 0 else (frame_id - 1) / fps_in
                do_infer = (frame_id - 1) % args.frame_skip == 0

            annotated = frame.copy() if anotar else frame

            if do_infer:
                processed += 1
                try:
                    results = alpr.predict(frame) or []
                except Exception as exc:  # noqa: BLE001 — un frame malo no debe abortar el run
                    LOG.error("Fallo de inferencia en frame %d: %s", frame_id, exc)
                    results = []

                for raw in results:
                    hit = parse_result(raw)
                    if hit is None:
                        continue
                    if hit.score <= args.min_confidence:
                        LOG.debug("Descartada %s (score %.3f)", hit.text, hit.score)
                        continue
                    detections += 1
                    if anotar:
                        draw_hit(annotated, hit)
                    is_new = dedup.accept(hit.text, t_stream)
                    crop_path = frame_path = ""
                    if crops is not None and (is_new or args.save_all_crops):
                        crop_path, frame_path = crops.save(frame, hit, frame_id)
                    if is_new:
                        csv_log.write(hit, frame_id, t_stream, crop_path, frame_path)
                        LOG.info(
                            "[%s] placa=%s score=%.3f frame=%d%s",
                            hms(t_stream), hit.text, hit.score, frame_id,
                            f" -> {Path(crop_path).name}" if crop_path else "",
                        )
                last_annotated = annotated
            elif anotar and last_annotated is not None:
                # Reutiliza las cajas del último frame inferido para continuidad visual.
                annotated = last_annotated.copy()

            if writer is not None or vista is not None:
                elapsed = time.perf_counter() - t0
                if args.hud and anotar:
                    live_info = ""
                    if reader is not None:
                        live_info = f"drop {reader.dropped} | rec {reader.reconnects}"
                    draw_hud(annotated, frame_id, t_stream, csv_log.rows,
                             frame_id / elapsed if elapsed else 0.0, live_info)
                if writer is not None:
                    writer.write(annotated)
                if vista is not None:
                    vista.publicar(annotated)

            if args.preview:
                cv2.imshow("fast-alpr", annotated)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break

            if args.max_frames and frame_id >= args.max_frames:
                break
            if args.duration and (time.perf_counter() - t0) >= args.duration:
                LOG.info("Duración máxima alcanzada (%.0fs).", args.duration)
                break
            if frame_id % args.progress_every == 0:
                el = time.perf_counter() - t0
                extra = ""
                if reader is not None:
                    extra = f" | descartados={reader.dropped + skipped_live} reconexiones={reader.reconnects}"
                LOG.info("Progreso: %d frames | %.1f fps | %d filas CSV%s",
                         frame_id, frame_id / el, csv_log.rows, extra)
    finally:
        if reader is not None:
            reader.stop()
        if cap is not None:
            cap.release()
        if writer is not None:
            writer.release()
        if args.preview:
            cv2.destroyAllWindows()
        csv_log.close()

    el = max(time.perf_counter() - t0, 1e-6)
    LOG.info(
        "Fin. frames=%d inferidos=%d detecciones=%d filas_csv=%d (%.1f fps, %.1fs)",
        frame_id, processed, detections, csv_log.rows, frame_id / el, el,
    )
    if reader is not None:
        LOG.info("Stream: frames descartados=%d (obsoletos) + %d (limitador) | reconexiones=%d",
                 reader.dropped, skipped_live, reader.reconnects)
    if crops is not None:
        LOG.info("Recortes guardados=%d fallidos=%d en %s", crops.saved, crops.failed, crops.root.resolve())
    if vista is not None:
        LOG.info("Vista en vivo: %d fotogramas publicados (%d fallidos)",
                 vista.publicados, vista.fallidos)
        vista.cerrar()
    LOG.info("CSV: %s", Path(args.csv).resolve())
    if writer is not None:
        for p in writer.paths:
            LOG.info("Video anotado: %s", p.resolve())
    return 0


def _env(name: str, default: Any) -> Any:
    """Valor por defecto desde ALPR_<NAME> (usado por el EnvironmentFile de systemd)."""
    raw = os.environ.get(f"ALPR_{name.upper()}")
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "y", "on", "si", "sí")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ALPR de video y cámaras RTSP en tiempo real (fast-alpr + OpenCV). "
                    "Cada opción acepta también la variable de entorno ALPR_<OPCION>.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-i", "--input", default=_env("input", None),
                   help="Ruta de video, URL RTSP/HTTP o índice de cámara. "
                        "Si se omite: cámara trasera en Android, menú de selección en PC")

    src = p.add_argument_group("fuente de video (multiplataforma)")
    src.add_argument("--select-source", action="store_true", default=_env("select_source", False),
                     help="Forzar el menú de selección aunque exista ALPR_INPUT")
    src.add_argument("--list-cameras", action="store_true",
                     help="Enumerar cámaras detectadas y salir")
    src.add_argument("--preview-camera", type=int, metavar="N",
                     help="Vista previa de la cámara N (segun --list-cameras) y salir")
    src.add_argument("--scan-ip-cameras", action="store_true", default=_env("scan_ip_cameras", False),
                     help="Buscar cámaras IP en la red local durante la detección")
    src.add_argument("--ip-subnet", action="append", default=None, metavar="CIDR",
                     help="Subred a explorar (repetible), p. ej. 192.168.1.0/24")
    src.add_argument("--no-preview", action="store_true", default=_env("no_preview", False),
                     help="No ofrecer vista previa en el menú de selección")
    p.add_argument("-o", "--output", default=_env("output", "output_annotated.mp4"),
                   help="Video anotado de salida")
    p.add_argument("-c", "--csv", default=_env("csv", "plates_log.csv"),
                   help="CSV de detecciones (modo append)")

    g = p.add_argument_group("detección")
    g.add_argument("--min-confidence", type=float, default=_env("min_confidence", 0.8),
                   help="Umbral estricto (>) de confianza")
    g.add_argument("--dedup-window", type=float, default=_env("dedup_window", 5.0),
                   help="Ventana anti-duplicados en segundos")
    g.add_argument("--frame-skip", type=int, default=_env("frame_skip", 1),
                   help="Inferir 1 de cada N frames (solo archivo)")
    g.add_argument("--detector-model", default=_env("detector_model", "yolo-v9-t-384-license-plate-end2end"))
    g.add_argument("--ocr-model", default=_env("ocr_model", "cct-xs-v1-global-model"))

    im = p.add_argument_group("imágenes de placas")
    im.add_argument("--save-crops", action="store_true", default=_env("save_crops", False),
                    help="Guardar un JPEG por placa detectada")
    im.add_argument("--crops-dir", default=_env("crops_dir", "crops"), help="Directorio raíz de recortes")
    im.add_argument("--crop-margin", type=float, default=_env("crop_margin", 0.15),
                    help="Margen extra alrededor del box (0.15 = 15%%)")
    im.add_argument("--jpeg-quality", type=int, default=_env("jpeg_quality", 92), help="Calidad JPEG 1-100")
    im.add_argument("--crop-min-size", type=int, default=_env("crop_min_size", 16),
                    help="Descartar recortes menores a N px de lado")
    im.add_argument("--save-full-frame", action="store_true", default=_env("save_full_frame", False),
                    help="Guardar también el frame completo de cada evento")
    im.add_argument("--save-all-crops", action="store_true", default=_env("save_all_crops", False),
                    help="Guardar toda detección, ignorando la ventana anti-duplicados")
    im.add_argument("--crops-by-plate", action="store_true", default=_env("crops_by_plate", False),
                    help="Subcarpeta por matrícula dentro de la fecha")

    live = p.add_argument_group("tiempo real (RTSP / webcam)")
    live.add_argument("--live", action="store_true", default=_env("live", False), help="Forzar modo en vivo")
    live.add_argument("--no-live", action="store_true", help="Forzar modo archivo (sin hilo ni descartes)")
    live.add_argument("--target-fps", type=float, default=_env("target_fps", 0.0),
                      help="Límite de frames inferidos por segundo (0 = todos los que lleguen)")
    live.add_argument("--rtsp-transport", default=_env("rtsp_transport", "tcp"), choices=["tcp", "udp"],
                      help="Transporte RTSP")
    live.add_argument("--rtsp-timeout", type=float, default=_env("rtsp_timeout", 10.0),
                      help="Timeout de socket RTSP en segundos")
    live.add_argument("--buffer-size", type=int, default=1, help="CAP_PROP_BUFFERSIZE (baja latencia = 1)")
    live.add_argument("--reconnect-delay", type=float, default=1.0, help="Espera inicial de reconexión")
    live.add_argument("--max-reconnect-delay", type=float, default=30.0, help="Tope del backoff")
    live.add_argument("--read-timeout", type=float, default=15.0, help="Alerta si no llegan frames en N s")
    live.add_argument("--duration", type=float, default=_env("duration", 0.0),
                      help="Detener tras N segundos (0 = indefinido)")
    live.add_argument("--keep-recording-offline", action="store_true",
                      default=_env("keep_recording_offline", False),
                      help="Seguir grabando el último frame con aviso mientras reconecta")

    out = p.add_argument_group("salida")
    out.add_argument("--segment-minutes", type=float, default=_env("segment_minutes", 0.0),
                     help="Rotar el video cada N minutos (0 = archivo único)")
    out.add_argument("--record-fps", type=float, default=_env("record_fps", 0.0),
                     help="FPS del video de salida (0 = automático)")
    out.add_argument("--fourcc", default=_env("fourcc", "mp4v"), help="Codec de salida (mp4v, avc1, XVID)")
    out.add_argument("--fallback-fps", type=float, default=25.0, help="FPS si la fuente no lo reporta")
    out.add_argument("--max-frames", type=int, default=0, help="Límite de frames (0 = sin límite)")
    out.add_argument("--progress-every", type=int, default=200, help="Frecuencia del log de progreso")
    out.add_argument("--no-video", action="store_true", default=_env("no_video", False),
                     help="Solo CSV, sin escribir video")
    out.add_argument("--preview", action="store_true", help="Ventana en vivo (q/ESC para salir)")
    out.add_argument("--hud", action="store_true", default=_env("hud", False),
                     help="Superponer contadores y fecha/hora")

    vv = p.add_argument_group("vista en vivo para el panel web")
    vv.add_argument("--live-view", default=_env("live_view", None), metavar="RUTA.jpg",
                    help="Publicar el último fotograma anotado como JPEG en esa ruta "
                         "(lo consume el panel web); vacío = desactivado")
    vv.add_argument("--live-view-fps", type=float, default=_env("live_view_fps", 3.0),
                    help="Fotogramas por segundo de la vista en vivo")
    vv.add_argument("--live-view-width", type=int, default=_env("live_view_width", 640),
                    help="Ancho máximo del JPEG publicado (se reescala manteniendo proporción)")
    vv.add_argument("--live-view-quality", type=int, default=_env("live_view_quality", 70),
                    help="Calidad JPEG de la vista en vivo (1-100)")
    out.add_argument("-v", "--verbose", action="store_true", default=_env("verbose", False))

    args = p.parse_args(argv)
    if not args.input and not is_interactive_platform() and not args.list_cameras and args.preview_camera is None:
        p.error(
            "falta la fuente: usa -i/--input o define ALPR_INPUT (p. ej. en /etc/alpr/alpr.env). "
            "El menú interactivo requiere una consola."
        )
    if not 1 <= args.jpeg_quality <= 100:
        p.error("--jpeg-quality debe estar entre 1 y 100")
    if not 1 <= args.live_view_quality <= 100:
        p.error("--live-view-quality debe estar entre 1 y 100")
    if args.live_view_fps < 0:
        p.error("--live-view-fps no puede ser negativo")
    if args.live_view_width < 160:
        p.error("--live-view-width debe ser al menos 160")
    if args.frame_skip < 1:
        p.error("--frame-skip debe ser >= 1")
    if not 0.0 <= args.min_confidence <= 1.0:
        p.error("--min-confidence debe estar entre 0 y 1")
    if args.live and args.no_live:
        p.error("--live y --no-live son mutuamente excluyentes")
    return args


def is_interactive_platform() -> bool:
    """¿Podemos resolver la fuente sin -i? (Android automático o consola en PC)"""
    if not HAS_SOURCE_MODULE:
        return False
    try:
        from video_source import is_interactive as _tty

        return detect_platform() == "android" or _tty()
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    if args.list_cameras or args.preview_camera is not None:
        if not HAS_SOURCE_MODULE:
            LOG.error("video_source.py no disponible: %s", _SOURCE_IMPORT_ERROR)
            return 1
        LOG.info("Plataforma detectada: %s", detect_platform())
        cams = enumerate_cameras(include_ip=args.scan_ip_cameras, ip_subnets=args.ip_subnet)
        if args.preview_camera is not None:
            if not 1 <= args.preview_camera <= len(cams):
                LOG.error("Cámara %d fuera de rango (1-%d)", args.preview_camera, len(cams))
                return 1
            return 0 if preview_camera(cams[args.preview_camera - 1]) else 1
        if not cams:
            LOG.error("No se detectó ninguna cámara")
            return 1
        return 0

    try:
        return run(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        LOG.exception("Error fatal: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
