#!/usr/bin/env python3
"""Supervisor de procesos ALPR para hosts sin systemd (Windows, macOS, proot).

Arranca un proceso ``alpr_stream.py`` por cámara, lo vigila y lo reinicia con
backoff si termina de forma inesperada — el equivalente práctico de
``Restart=always`` de la unidad systemd usada en Linux.

Cada cámara produce:

    <logs>/<id>.log     salida combinada del proceso (stdout + stderr)
    <run>/<id>.pid      PID y metadatos en JSON, para reenganchar tras reiniciar el panel
    <config>/<id>.env   variables de entorno específicas de la cámara (opcional)

El estado se expone con la misma forma que ``systemctl show`` traducido, para
que el panel web no distinga el backend que tiene debajo.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("alpr.web.supervisor")

ES_WINDOWS = os.name == "nt"

# Espera entre reintentos: crece hasta el último valor y se mantiene ahí.
BACKOFF = (5, 5, 10, 20, 30, 60)
# Un proceso que sobrevive este tiempo se considera arranque correcto.
ARRANQUE_ESTABLE = 30.0
# Periodo del hilo de vigilancia.
INTERVALO_VIGILANCIA = 2.0


def _pid_vivo(pid: int) -> bool:
    """True si el PID existe, sin enviar señales que lo alteren."""
    if pid <= 0:
        return False
    if ES_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            codigo = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)) == 0:
                return False
            return codigo.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _leer_env_file(ruta: Path) -> dict[str, str]:
    """Lee un archivo estilo ``KEY=valor`` ignorando comentarios y vacíos."""
    valores: dict[str, str] = {}
    try:
        texto = ruta.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        LOG.warning("No se pudo leer %s: %s", ruta, exc)
        return valores
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        clave = clave.strip()
        if clave.lower().startswith("export "):
            clave = clave[7:].strip()
        valor = valor.strip().strip('"').strip("'")
        if clave:
            valores[clave] = valor
    return valores


@dataclass
class Proceso:
    """Estado en memoria de una cámara supervisada."""

    camera_id: str
    popen: subprocess.Popen | None = None
    pid: int = 0
    inicio: float = 0.0
    reinicios: int = 0
    intento: int = 0
    detencion_pedida: bool = False
    ultimo_error: str = ""
    proximo_intento: float = 0.0
    log: Path | None = None
    handle_log: Any = None
    externo: bool = False

    def vivo(self) -> bool:
        if self.popen is not None:
            return self.popen.poll() is None
        return self.pid > 0 and _pid_vivo(self.pid)

    def cerrar_log(self) -> None:
        if self.handle_log is not None:
            try:
                self.handle_log.close()
            except OSError:
                pass
            self.handle_log = None


class Supervisor:
    """Gestiona los procesos ALPR de todas las cámaras del panel."""

    def __init__(
        self,
        *,
        python_exe: str,
        script: Path,
        project_dir: Path,
        data_dir: Path,
        config_dir: Path,
        log_dir: Path,
        run_dir: Path,
        live_dir: Path | None = None,
        live_fps: float = 3.0,
        live_width: int = 640,
        live_quality: int = 70,
        base_env_file: Path | None = None,
        autoreiniciar: bool = True,
        max_reinicios: int = 0,
    ) -> None:
        self.python_exe = python_exe
        self.script = script
        self.project_dir = project_dir
        self.data_dir = data_dir
        self.config_dir = config_dir
        self.log_dir = log_dir
        self.run_dir = run_dir
        self.live_dir = live_dir or (data_dir / "live")
        self.live_fps = live_fps
        self.live_width = live_width
        self.live_quality = live_quality
        self.base_env_file = base_env_file
        self.autoreiniciar = autoreiniciar
        self.max_reinicios = max_reinicios  # 0 = sin límite
        self._procesos: dict[str, Proceso] = {}
        self._camaras: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        for carpeta in (self.log_dir, self.run_dir, self.config_dir):
            carpeta.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ ciclo
    def iniciar_vigilancia(self) -> None:
        if self._hilo and self._hilo.is_alive():
            return
        self._parar.clear()
        self._hilo = threading.Thread(target=self._vigilar, name="alpr-vigilancia", daemon=True)
        self._hilo.start()
        LOG.info("Vigilancia de procesos activa (autoreinicio=%s)", self.autoreiniciar)

    def detener_vigilancia(self) -> None:
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout=5)

    def apagar(self, detener_procesos: bool = False) -> None:
        """Cierra la vigilancia. Los procesos siguen vivos salvo que se pida lo contrario."""
        self.detener_vigilancia()
        with self._lock:
            ids = list(self._procesos)
        if detener_procesos:
            for camera_id in ids:
                try:
                    self.detener(camera_id)
                except Exception as exc:  # pragma: no cover - apagado best-effort
                    LOG.warning("Error deteniendo %s: %s", camera_id, exc)
        with self._lock:
            for proc in self._procesos.values():
                proc.cerrar_log()

    def _vigilar(self) -> None:
        while not self._parar.wait(INTERVALO_VIGILANCIA):
            ahora = time.monotonic()
            with self._lock:
                pendientes = [
                    (pid_, proc) for pid_, proc in self._procesos.items()
                    if not proc.detencion_pedida
                ]
            for camera_id, proc in pendientes:
                if proc.vivo():
                    if proc.inicio and ahora - proc.inicio > ARRANQUE_ESTABLE:
                        proc.intento = 0
                    continue
                codigo = proc.popen.returncode if proc.popen else None
                if not self.autoreiniciar:
                    proc.ultimo_error = f"proceso terminado (código {codigo})"
                    continue
                if self.max_reinicios and proc.reinicios >= self.max_reinicios:
                    proc.ultimo_error = (
                        f"detenido tras {proc.reinicios} reinicios (código {codigo})"
                    )
                    proc.detencion_pedida = True
                    continue
                if ahora < proc.proximo_intento:
                    continue
                espera = BACKOFF[min(proc.intento, len(BACKOFF) - 1)]
                proc.intento += 1
                proc.reinicios += 1
                proc.proximo_intento = ahora + espera
                camara = self._camaras.get(camera_id)
                if camara is None:
                    proc.ultimo_error = "cámara ya no existe en el almacén"
                    proc.detencion_pedida = True
                    continue
                LOG.warning(
                    "Cámara %s terminó (código %s). Reinicio %d en curso, backoff %ds",
                    camera_id, codigo, proc.reinicios, espera,
                )
                try:
                    self._lanzar(camara, reinicio=True)
                except Exception as exc:
                    proc.ultimo_error = f"reinicio falló: {exc}"
                    LOG.error("Reinicio de %s falló: %s", camera_id, exc)

    # ----------------------------------------------------------------- lanzar
    def registrar(self, camaras: list[dict[str, Any]]) -> None:
        """Cachea la definición de cámaras para poder reiniciarlas sin el almacén."""
        with self._lock:
            self._camaras = {c["id"]: dict(c) for c in camaras}

    def entorno_camara(self, camara: dict[str, Any]) -> dict[str, str]:
        """Variables ALPR_* efectivas para una cámara: base + archivo propio + panel."""
        entorno = dict(os.environ)
        if self.base_env_file and self.base_env_file.is_file():
            entorno.update(_leer_env_file(self.base_env_file))
        propio = self.config_dir / f"{camara['id']}.env"
        if propio.is_file():
            entorno.update(_leer_env_file(propio))
        salida = self.data_dir / "crops"
        entorno.update({
            "ALPR_INPUT": str(camara["fuente"]),
            "ALPR_MIN_CONFIDENCE": str(camara.get("min_confianza", 0.8)),
            "ALPR_TARGET_FPS": str(camara.get("fps_objetivo", 8)),
            "ALPR_OUTPUT_DIR": entorno.get("ALPR_OUTPUT_DIR", str(self.data_dir)),
            "ALPR_CSV": entorno.get("ALPR_CSV", str(self.data_dir / "placas.csv")),
            "ALPR_CROPS_DIR": entorno.get("ALPR_CROPS_DIR", str(salida)),
            "ALPR_CAMERA_ID": camara["id"],
            # Vista en vivo del panel: el motor publica aquí el último fotograma.
            "ALPR_LIVE_VIEW": str(self.live_dir / f"{camara['id']}.jpg"),
            "ALPR_LIVE_VIEW_FPS": f"{self.live_fps:g}",
            "ALPR_LIVE_VIEW_WIDTH": str(self.live_width),
            "ALPR_LIVE_VIEW_QUALITY": str(self.live_quality),
            "ALPR_CAMERA_NAME": camara.get("nombre", camara["id"]),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        if camara.get("tipo") == "rtsp":
            entorno.setdefault("ALPR_RTSP_TRANSPORT", "tcp")
        return entorno

    def _lanzar(self, camara: dict[str, Any], reinicio: bool = False) -> Proceso:
        camera_id = camara["id"]
        if not self.script.is_file():
            raise FileNotFoundError(f"No se encuentra el motor ALPR en {self.script}")
        for carpeta in (self.log_dir, self.run_dir, self.live_dir):
            carpeta.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{camera_id}.log"
        handle = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        marca = time.strftime("%Y-%m-%d %H:%M:%S")
        handle.write(
            f"\n===== {marca} {'reinicio' if reinicio else 'arranque'} "
            f"de {camera_id} ({camara.get('nombre', '')}) =====\n"
        )
        cmd = [self.python_exe, str(self.script)]
        extra: dict[str, Any] = {}
        if ES_WINDOWS:
            # Sin ventana de consola y en su propio grupo, para poder cerrarlo entero.
            extra["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            extra["start_new_session"] = True
        popen = subprocess.Popen(
            cmd,
            cwd=str(self.project_dir),
            env=self.entorno_camara(camara),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **extra,
        )
        with self._lock:
            anterior = self._procesos.get(camera_id)
            proc = anterior or Proceso(camera_id=camera_id)
            proc.cerrar_log()
            proc.popen = popen
            proc.pid = popen.pid
            proc.inicio = time.monotonic()
            proc.detencion_pedida = False
            proc.externo = False
            proc.log = log_path
            proc.handle_log = handle
            proc.ultimo_error = ""
            if not reinicio:
                proc.intento = 0
                proc.reinicios = 0
            self._procesos[camera_id] = proc
            self._camaras[camera_id] = dict(camara)
        self._escribir_pid(camera_id, popen.pid, camara)
        LOG.info("Cámara %s lanzada con PID %d (log %s)", camera_id, popen.pid, log_path)
        return proc

    def iniciar(self, camara: dict[str, Any]) -> dict[str, Any]:
        camera_id = camara["id"]
        with self._lock:
            proc = self._procesos.get(camera_id)
        if proc and proc.vivo():
            return {"ok": True, "accion": "iniciar", "detalle": "ya estaba en ejecución"}
        self._lanzar(camara)
        time.sleep(0.6)  # margen para detectar un fallo inmediato de arranque
        with self._lock:
            proc = self._procesos[camera_id]
        if not proc.vivo():
            cola = self.registro(camera_id, lineas=15).get("contenido", "")
            raise RuntimeError(
                f"El proceso terminó de inmediato (código {proc.popen.returncode if proc.popen else '?'}). "
                f"Últimas líneas del registro:\n{cola[-800:]}"
            )
        self.iniciar_vigilancia()
        return {"ok": True, "accion": "iniciar", "detalle": f"PID {proc.pid}"}

    def detener(self, camera_id: str, timeout: float = 12.0) -> dict[str, Any]:
        with self._lock:
            proc = self._procesos.get(camera_id)
            if proc is None:
                datos = self._leer_pid(camera_id)
                if datos and _pid_vivo(int(datos.get("pid", 0))):
                    proc = Proceso(camera_id=camera_id, pid=int(datos["pid"]), externo=True)
                    self._procesos[camera_id] = proc
            if proc is None or not proc.vivo():
                self._borrar_pid(camera_id)
                return {"ok": True, "accion": "detener", "detalle": "no estaba en ejecución"}
            proc.detencion_pedida = True

        if proc.popen is not None:
            proc.popen.terminate()
            try:
                proc.popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                LOG.warning("Cámara %s no respondió al cierre: forzando", camera_id)
                self._matar_arbol(proc.pid)
                try:
                    proc.popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.popen.kill()
        else:
            self._matar_arbol(proc.pid)
            fin = time.monotonic() + timeout
            while _pid_vivo(proc.pid) and time.monotonic() < fin:
                time.sleep(0.3)

        proc.cerrar_log()
        self._borrar_pid(camera_id)
        self._borrar_vista(camera_id)
        LOG.info("Cámara %s detenida", camera_id)
        return {"ok": True, "accion": "detener", "detalle": "proceso detenido"}

    def _borrar_vista(self, camera_id: str) -> None:
        """Elimina el JPEG de la vista para que el panel muestre «sin señal»."""
        for nombre in (f"{camera_id}.jpg", f"{camera_id}.jpg.tmp"):
            try:
                (self.live_dir / nombre).unlink()
            except OSError:
                pass

    def reiniciar(self, camara: dict[str, Any]) -> dict[str, Any]:
        self.detener(camara["id"])
        time.sleep(0.5)
        resultado = self.iniciar(camara)
        return {**resultado, "accion": "reiniciar"}

    def _matar_arbol(self, pid: int) -> None:
        if pid <= 0:
            return
        if ES_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, check=False, timeout=15,
            )
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    # ------------------------------------------------------------------ estado
    def estado(self, camera_id: str) -> dict[str, Any]:
        """Mismo contrato que el backend systemd del panel."""
        with self._lock:
            proc = self._procesos.get(camera_id)
        if proc is None:
            datos = self._leer_pid(camera_id)
            if datos and _pid_vivo(int(datos.get("pid", 0))):
                proc = Proceso(
                    camera_id=camera_id, pid=int(datos["pid"]), externo=True,
                    log=self.log_dir / f"{camera_id}.log",
                )
                with self._lock:
                    self._procesos[camera_id] = proc
        if proc is None:
            return {
                "unidad": f"alpr:{camera_id}", "disponible": True, "estado": "inactive",
                "subestado": "dead", "activa": False, "reinicios": 0, "pid": 0,
                "desde": "", "detalle": "nunca iniciada desde el panel", "backend": "proceso",
                "registro": str(self.log_dir / f"{camera_id}.log"),
            }
        vivo = proc.vivo()
        codigo = proc.popen.returncode if (proc.popen and not vivo) else None
        detalle = proc.ultimo_error or (
            "en ejecución" if vivo else f"terminado (código {codigo})"
        )
        if vivo and proc.externo:
            detalle = "en ejecución (reenganchada por PID)"
        desde = ""
        if vivo and proc.inicio:
            desde = f"{int(time.monotonic() - proc.inicio)} s"
        return {
            "unidad": f"alpr:{camera_id}",
            "disponible": True,
            "estado": "active" if vivo else "inactive",
            "subestado": "running" if vivo else "dead",
            "activa": vivo,
            "reinicios": proc.reinicios,
            "pid": proc.pid if vivo else 0,
            "desde": desde,
            "detalle": detalle,
            "backend": "proceso",
            "registro": str(proc.log or (self.log_dir / f"{camera_id}.log")),
        }

    def registro(self, camera_id: str, lineas: int = 200) -> dict[str, Any]:
        """Últimas líneas del log de una cámara."""
        ruta = self.log_dir / f"{camera_id}.log"
        if not ruta.is_file():
            return {"ruta": str(ruta), "existe": False, "contenido": ""}
        try:
            with ruta.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                tam = fh.tell()
                bloque = min(tam, max(4096, lineas * 220))
                fh.seek(tam - bloque)
                datos = fh.read().decode("utf-8", errors="replace")
        except OSError as exc:
            return {"ruta": str(ruta), "existe": True, "contenido": f"[error al leer: {exc}]"}
        cola = datos.splitlines()[-lineas:]
        return {"ruta": str(ruta), "existe": True, "contenido": "\n".join(cola)}

    # -------------------------------------------------------------- pid files
    def _pid_file(self, camera_id: str) -> Path:
        return self.run_dir / f"{camera_id}.pid"

    def _escribir_pid(self, camera_id: str, pid: int, camara: dict[str, Any]) -> None:
        try:
            self._pid_file(camera_id).write_text(
                json.dumps({
                    "pid": pid,
                    "camara": camera_id,
                    "nombre": camara.get("nombre", ""),
                    "inicio": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "panel_pid": os.getpid(),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            LOG.warning("No se pudo escribir el PID de %s: %s", camera_id, exc)

    def _leer_pid(self, camera_id: str) -> dict[str, Any] | None:
        ruta = self._pid_file(camera_id)
        if not ruta.is_file():
            return None
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _borrar_pid(self, camera_id: str) -> None:
        try:
            self._pid_file(camera_id).unlink(missing_ok=True)
        except OSError:
            pass

    def reenganchar(self, camaras: list[dict[str, Any]]) -> int:
        """Adopta procesos que sobrevivieron a un reinicio del panel."""
        self.registrar(camaras)
        adoptadas = 0
        for camara in camaras:
            datos = self._leer_pid(camara["id"])
            if not datos:
                continue
            pid = int(datos.get("pid", 0))
            if _pid_vivo(pid):
                with self._lock:
                    self._procesos[camara["id"]] = Proceso(
                        camera_id=camara["id"], pid=pid, externo=True,
                        inicio=time.monotonic(),
                        log=self.log_dir / f"{camara['id']}.log",
                    )
                adoptadas += 1
                LOG.info("Cámara %s reenganchada con PID %d", camara["id"], pid)
            else:
                self._borrar_pid(camara["id"])
        if adoptadas:
            self.iniciar_vigilancia()
        return adoptadas

    def escribir_env(self, camara: dict[str, Any], contenido: str) -> Path:
        """Guarda el archivo .env propio de una cámara en el directorio de configuración."""
        ruta = self.config_dir / f"{camara['id']}.env"
        tmp = ruta.with_suffix(".env.tmp")
        tmp.write_text(contenido, encoding="utf-8")
        tmp.replace(ruta)
        if not ES_WINDOWS:
            os.chmod(ruta, 0o600)
        LOG.info("Configuración de %s escrita en %s", camara["id"], ruta)
        return ruta


def python_del_entorno() -> str:
    """Intérprete a usar para los hijos: el del venv que ejecuta el panel."""
    return os.environ.get("ALPR_WEB_PYTHON") or sys.executable
