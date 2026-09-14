#!/usr/bin/env python3
"""Genera datos sintéticos para probar el panel web sin cámaras reales.

Crea en ``web/demo-datos`` un ``camaras.json``, un ``placas.csv`` con el mismo
formato que produce ``alpr_stream.py`` y un recorte JPEG por detección.

    python web/generar_demo.py [--horas 48] [--detecciones 420]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent
DATA = BASE / "demo-datos"

CSV_FIELDS = [
    "camera_id", "plate", "frame_id", "crop_path", "frame_path", "stream_timestamp_s",
    "stream_timestamp_hms", "wallclock_local", "wallclock_utc", "source",
    "ocr_confidence", "detection_confidence", "x1", "y1", "x2", "y2",
]

CAMARAS = [
    {"id": "acceso-norte", "nombre": "Acceso norte", "fuente": "rtsp://operador:***@192.168.1.40:554/Streaming/Channels/101",
     "tipo": "rtsp", "min_confianza": 0.85, "fps_objetivo": 8.0, "activa": True,
     "notas": "Barrera de entrada, carril único, iluminación IR"},
    {"id": "salida-sur", "nombre": "Salida sur", "fuente": "rtsp://operador:***@192.168.1.41:554/Streaming/Channels/101",
     "tipo": "rtsp", "min_confianza": 0.82, "fps_objetivo": 8.0, "activa": True,
     "notas": "Doble carril, contraluz al atardecer"},
    {"id": "muelle-carga", "nombre": "Muelle de carga", "fuente": "rtsp://operador:***@192.168.1.42:8554/live",
     "tipo": "rtsp", "min_confianza": 0.88, "fps_objetivo": 5.0, "activa": True,
     "notas": "Camiones, placas de mayor tamaño"},
    {"id": "garita-usb", "nombre": "Garita (webcam USB)", "fuente": "0",
     "tipo": "usb", "min_confianza": 0.80, "fps_objetivo": 12.0, "activa": False,
     "notas": "Puesto de vigilancia, portátil Linux"},
]

# Placas ficticias con formato venezolano y de carga.
FLOTA = ["AB123CD", "XY987ZW", "MN456PQ", "JK321LM", "RS654TU", "CD789EF",
         "GH147IJ", "KL258MN", "A12BC3D", "PQ963RS", "TU852VW", "XY741ZA"]
FRECUENTES = ["AB123CD", "MN456PQ", "RS654TU"]

FONT = cv2.FONT_HERSHEY_SIMPLEX


def recorte_placa(texto: str, ancho: int = 320, alto: int = 110) -> "np.ndarray":
    """Dibuja un recorte de placa plausible (fondo, borde, ruido y viñeteado)."""
    fondo = random.randint(196, 232)
    img = np.full((alto, ancho, 3), fondo, dtype=np.uint8)
    cv2.rectangle(img, (6, 6), (ancho - 7, alto - 7), (48, 48, 52), 3)
    escala = 2.0 if len(texto) <= 7 else 1.7
    (tw, th), _ = cv2.getTextSize(texto, FONT, escala, 5)
    org = ((ancho - tw) // 2, (alto + th) // 2)
    cv2.putText(img, texto, org, FONT, escala, (26, 26, 30), 5, cv2.LINE_AA)
    ruido = np.random.normal(0, 7, img.shape).astype(np.int16)
    img = np.clip(img.astype(np.int16) + ruido, 0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (3, 3), 0.6)
    ky = cv2.getGaussianKernel(alto, alto * 0.7)
    kx = cv2.getGaussianKernel(ancho, ancho * 0.7)
    mascara = (ky @ kx.T)
    mascara = 0.75 + 0.25 * (mascara / mascara.max())
    return np.clip(img * mascara[:, :, None], 0, 255).astype(np.uint8)


def factor_horario(hora: int) -> float:
    """Perfil de tráfico: picos de entrada y salida, madrugada tranquila."""
    perfil = {0: .05, 1: .03, 2: .03, 3: .04, 4: .08, 5: .25, 6: .55, 7: .95,
              8: 1.0, 9: .7, 10: .5, 11: .5, 12: .65, 13: .6, 14: .5, 15: .55,
              16: .8, 17: 1.0, 18: .85, 19: .5, 20: .3, 21: .2, 22: .12, 23: .08}
    return perfil.get(hora, .3)


def generar(horas: int, objetivo: int, semilla: int = 7) -> int:
    random.seed(semilla)
    np.random.seed(semilla)
    DATA.mkdir(parents=True, exist_ok=True)
    crops = DATA / "crops"
    if crops.exists():
        for p in crops.rglob("*.jpg"):
            p.unlink()
    crops.mkdir(parents=True, exist_ok=True)

    (DATA / "camaras.json").write_text(
        json.dumps(
            [{**c, "creada": (datetime.now().astimezone() - timedelta(days=21)).isoformat(timespec="seconds")}
             for c in CAMARAS],
            ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    activas = [c for c in CAMARAS if c["activa"]]
    pesos = [1.0, 0.8, 0.45]
    ahora = datetime.now().astimezone()
    inicio = ahora - timedelta(hours=horas)

    eventos: list[tuple[datetime, dict]] = []
    total_peso = sum(factor_horario((inicio + timedelta(hours=h)).hour) for h in range(horas))
    for h in range(horas):
        t0 = inicio + timedelta(hours=h)
        cuota = objetivo * factor_horario(t0.hour) / max(total_peso, 1e-9)
        for _ in range(int(cuota) + (random.random() < cuota % 1)):
            momento = t0 + timedelta(seconds=random.uniform(0, 3599))
            if momento > ahora:
                continue
            camara = random.choices(activas, weights=pesos[:len(activas)])[0]
            eventos.append((momento, camara))
    eventos.sort(key=lambda e: e[0])

    filas = []
    for i, (momento, camara) in enumerate(eventos):
        placa = random.choice(FRECUENTES if random.random() < 0.28 else FLOTA)
        ocr = round(min(0.999, random.gauss(0.93, 0.05)), 4)
        det = round(min(0.999, random.gauss(0.95, 0.03)), 4)
        if min(ocr, det) < camara["min_confianza"]:
            continue
        dia = momento.strftime("%Y-%m-%d")
        conf = int(min(ocr, det) * 100)
        frame_id = 30 + i * random.randint(40, 900)
        nombre = f"{momento.strftime('%H%M%S')}-{random.randint(0, 999):03d}_{placa}_{conf}_f{frame_id}.jpg"
        destino = crops / dia / nombre
        destino.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(destino), recorte_placa(placa), [cv2.IMWRITE_JPEG_QUALITY, 88])
        x1, y1 = random.randint(180, 900), random.randint(240, 620)
        filas.append({
            "camera_id": camara["id"],
            "plate": placa,
            "frame_id": frame_id,
            "crop_path": str(destino),
            "frame_path": "",
            "stream_timestamp_s": f"{frame_id / 8:.3f}",
            "stream_timestamp_hms": str(timedelta(seconds=int(frame_id / 8))),
            "wallclock_local": momento.isoformat(timespec="milliseconds"),
            "wallclock_utc": momento.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            "source": camara["fuente"],
            "ocr_confidence": f"{ocr:.4f}",
            "detection_confidence": f"{det:.4f}",
            "x1": x1, "y1": y1,
            "x2": x1 + random.randint(120, 260), "y2": y1 + random.randint(40, 90),
        })

    with (DATA / "placas.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(filas)

    print(f"{len(filas)} detecciones y recortes generados en {DATA}")
    return len(filas)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Datos de demostración del panel")
    p.add_argument("--horas", type=int, default=48)
    p.add_argument("--detecciones", type=int, default=420)
    a = p.parse_args()
    generar(a.horas, a.detecciones)
