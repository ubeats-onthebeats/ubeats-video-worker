#!/usr/bin/env python3
"""
Worker de generación de variantes de video — UBEATS
------------------------------------------------------
API HTTP que recibe una URL de video, genera N variantes con FFmpeg
(reutilizando la lógica de generar_variantes.py) y notifica a Hostinger
vía callback cuando termina.

Pensado para correr en Railway (Docker) donde SÍ hay binario de ffmpeg
y no hay restricciones de exec() como en el hosting compartido.
"""

import hashlib
import os
import random
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# --- Configuración vía variables de entorno (se setean en Railway) ---
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")  # ej: https://tudominio.com/admin-ubeats-2026/api/variantes-listas.php
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")  # token compartido para validar requests entrantes
STORAGE_DIR = os.environ.get("STORAGE_DIR", "/tmp/variantes")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")  # de dónde se sirven los archivos generados

Path(STORAGE_DIR).mkdir(parents=True, exist_ok=True)

# Estado en memoria de los jobs (para un uso personal/bajo volumen alcanza;
# si escala, mover a Redis o a la misma tabla de MySQL vía HTTP)
jobs = {}


# ---------------------------------------------------------------------
# Lógica de generación (misma base que generar_variantes.py)
# ---------------------------------------------------------------------

def random_transform_params():
    return {
        "crop_pct": round(random.uniform(0.01, 0.03), 4),
        "brightness": round(random.uniform(-0.02, 0.02), 4),
        "contrast": round(random.uniform(0.97, 1.03), 4),
        "saturation": round(random.uniform(0.95, 1.05), 4),
        "noise": random.randint(2, 6),
        "speed": round(random.uniform(0.99, 1.01), 4),
        "rotate_deg": round(random.uniform(-0.5, 0.5), 3),
        "bitrate_k": random.randint(3500, 6000),
    }


def build_filter_chain(params):
    filters = []

    if abs(params["rotate_deg"]) > 0.001:
        rad = params["rotate_deg"] * 3.14159265 / 180
        filters.append(f"rotate={rad}:fillcolor=black@0,crop=iw*0.98:ih*0.98")

    crop = params["crop_pct"]
    filters.append(
        f"crop=floor(iw*{1 - crop}/2)*2:floor(ih*{1 - crop}/2)*2,"
        f"scale=trunc(iw/{1 - crop}/2)*2:trunc(ih/{1 - crop}/2)*2"
    )

    filters.append(
        f"eq=brightness={params['brightness']}:contrast={params['contrast']}:"
        f"saturation={params['saturation']}"
    )
    filters.append(f"noise=alls={params['noise']}:allf=t")

    pts_factor = 1 / params["speed"]
    filters.append(f"setpts={pts_factor}*PTS")

    return ",".join(filters)


def generate_variant(input_path, output_path, params, crf=20, preset="veryfast"):
    vf = build_filter_chain(params)
    af = f"atempo={params['speed']}"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-b:v", f"{params['bitrate_k']}k",
        "-maxrate", f"{int(params['bitrate_k'] * 1.2)}k",
        "-bufsize", f"{int(params['bitrate_k'] * 2)}k",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map_metadata", "-1",
        "-metadata", f"creation_time={int(time.time())}",
        "-metadata", f"comment={uuid.uuid4().hex}",
        output_path,
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0, result.stderr.decode(errors="ignore")[-800:]


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_video(url, dest_path):
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)


def notify_callback(job_id, payload):
    if not CALLBACK_URL:
        return
    try:
        requests.post(
            CALLBACK_URL,
            json={"job_id": job_id, "secret": WORKER_SECRET, **payload},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[callback] error notificando job {job_id}: {e}")


# ---------------------------------------------------------------------
# Job en background
# ---------------------------------------------------------------------

def run_job(job_id, video_url, cantidad, crf, preset):
    job_dir = Path(STORAGE_DIR) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / "original.mp4"

    jobs[job_id]["estado"] = "descargando"
    try:
        download_video(video_url, input_path)
    except Exception as e:
        jobs[job_id]["estado"] = "error"
        jobs[job_id]["error"] = f"Error descargando video: {e}"
        notify_callback(job_id, {"estado": "error", "error": jobs[job_id]["error"]})
        return

    jobs[job_id]["estado"] = "procesando"
    variantes = []

    for i in range(1, cantidad + 1):
        params = random_transform_params()
        out_name = f"variante_{i}_{uuid.uuid4().hex[:6]}.mp4"
        out_path = job_dir / out_name

        ok, err = generate_variant(str(input_path), str(out_path), params, crf=crf, preset=preset)

        if ok:
            md5 = file_md5(out_path)
            variante_info = {
                "archivo": out_name,
                "url": f"{PUBLIC_BASE_URL}/{job_id}/{out_name}" if PUBLIC_BASE_URL else str(out_path),
                "md5": md5,
                "parametros": params,
            }
            variantes.append(variante_info)
            jobs[job_id]["variantes"] = variantes
            # Notificación incremental: cada variante lista se reporta al toque
            notify_callback(job_id, {"estado": "parcial", "variante": variante_info})
        else:
            print(f"[job {job_id}] error en variante {i}: {err}")

    jobs[job_id]["estado"] = "listo"
    jobs[job_id]["variantes"] = variantes
    notify_callback(job_id, {"estado": "listo", "variantes": variantes})


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

def check_auth(req):
    token = req.headers.get("X-Worker-Secret", "")
    return WORKER_SECRET and token == WORKER_SECRET


@app.route("/salud", methods=["GET"])
def salud():
    return jsonify({"status": "ok"})


@app.route("/procesar", methods=["POST"])
def procesar():
    if not check_auth(request):
        return jsonify({"error": "no autorizado"}), 401

    data = request.get_json(force=True, silent=True) or {}
    video_url = data.get("video_url")
    cantidad = int(data.get("cantidad", 6))
    crf = int(data.get("crf", 20))
    preset = data.get("preset", "veryfast")

    if not video_url:
        return jsonify({"error": "falta video_url"}), 400

    job_id = uuid.uuid4().hex
    jobs[job_id] = {"estado": "en_cola", "variantes": [], "error": None}

    thread = threading.Thread(
        target=run_job, args=(job_id, video_url, cantidad, crf, preset), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "estado": "en_cola"})


@app.route("/estado/<job_id>", methods=["GET"])
def estado(job_id):
    if not check_auth(request):
        return jsonify({"error": "no autorizado"}), 401

    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job no encontrado"}), 404

    return jsonify({"job_id": job_id, **job})


@app.route("/archivos/<job_id>/<filename>", methods=["GET"])
def archivos(job_id, filename):
    """Sirve los archivos generados (alternativa simple si no usás S3/Drive)."""
    from flask import send_from_directory
    job_dir = Path(STORAGE_DIR) / job_id
    return send_from_directory(job_dir, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
