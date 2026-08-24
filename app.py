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
import unicodedata
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# Fuente de los ganchos: se renderiza como imagen con Pillow (ver
# build_caption_overlay) en vez de con el filtro drawtext de ffmpeg, porque
# drawtext solo puede dibujar UN rectángulo recto que cubre todo el bloque de
# texto — acá queremos una caja negra con esquinas redondeadas por línea.
CAPTION_FONT_PATH = "/usr/share/fonts/truetype/poppins/Poppins-ExtraBold.ttf"

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
        "bitrate_k": random.randint(1800, 3200),
    }


def build_filter_chain(params, max_width=720):
    filters = []

    # Downscale primero: el uso de memoria de libx264 y de los filtros de
    # ruido/eq escala con la cantidad de píxeles por frame. Bajar la
    # resolución de trabajo es la palanca más efectiva contra OOM en
    # contenedores chicos (Railway starter con RAM limitada).
    filters.append(f"scale='min({max_width},iw)':'-2'")

    # Nota: se quitó la rotación — el filtro rotate+crop duplicaba el uso de
    # memoria en contenedores chicos (Railway starter) y causaba OOM kill.
    # El crop/zoom + ruido + brillo ya generan huella suficientemente distinta.

    crop = params["crop_pct"]
    filters.append(
        f"crop=floor(iw*{1 - crop}/2)*2:floor(ih*{1 - crop}/2)*2,"
        f"scale=trunc(iw/{1 - crop}/2)*2:trunc(ih/{1 - crop}/2)*2"
    )

    filters.append(
        f"eq=brightness={params['brightness']}:contrast={params['contrast']}:"
        f"saturation={params['saturation']}"
    )
    filters.append(f"noise=alls={params['noise']}:allf=t+u")

    pts_factor = 1 / params["speed"]
    filters.append(f"setpts={pts_factor}*PTS")

    return ",".join(filters)


def generate_variant(input_path, output_path, params, crf=20, preset="veryfast"):
    vf = build_filter_chain(params)
    af = f"atempo={params['speed']}"

    cmd = [
        "ffmpeg", "-y",
        "-threads", "1",  # limita picos de memoria en contenedores chicos (Railway starter)
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
    err_detail = result.stderr.decode(errors="ignore")[-800:]
    if result.returncode != 0:
        err_detail = f"[returncode={result.returncode}] {err_detail}"
    return result.returncode == 0, err_detail


# ---------------------------------------------------------------------
# Lógica de ganchos (misma base que generar_ganchos.py) — cada salida es un
# video con un mensaje de apertura DISTINTO quemado como overlay de texto,
# para testear qué gancho retiene mejor. Contenido real y perceptible, no
# una copia disfrazada del mismo mensaje.
# ---------------------------------------------------------------------

def slugify(text, max_len=30):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = "".join(c if c.isalnum() else "_" for c in text)
    return text.strip("_")[:max_len].lower() or "gancho"


def wrap_text(text, max_chars_per_line=16):
    """Parte el texto en líneas para que no se corte en los bordes del video."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars_per_line and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines)


def build_caption_overlay(
    gancho_texto, out_path,
    font_size=54, max_chars_per_line=22,
    text_color=(255, 255, 255, 255), box_color=(0, 0, 0, 178),
    pad_x=26, pad_y=16, line_gap=12, corner_radius=20,
):
    """Renderiza el gancho como PNG transparente: una caja negra con esquinas
    redondeadas pegada a CADA línea de texto (look estilo captions de Reels/TikTok),
    en vez del rectángulo recto único que hace drawtext de ffmpeg. Se guarda en
    out_path y se superpone al video después con el filtro overlay."""
    lineas = wrap_text(gancho_texto, max_chars_per_line=max_chars_per_line).split("\n")
    font = ImageFont.truetype(CAPTION_FONT_PATH, font_size)

    tmp_draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    line_boxes = []  # (ancho_texto, alto_texto, bbox)
    for linea in lineas:
        bbox = tmp_draw.textbbox((0, 0), linea, font=font)
        line_boxes.append((bbox[2] - bbox[0], bbox[3] - bbox[1], bbox))

    ancho_total = max(w for w, h, _ in line_boxes) + pad_x * 2
    alto_total = sum(h + pad_y * 2 for w, h, _ in line_boxes) + line_gap * (len(lineas) - 1)

    img = Image.new("RGBA", (int(ancho_total), int(alto_total)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    y = 0
    for (ancho_txt, alto_txt, bbox), linea in zip(line_boxes, lineas):
        box_w = ancho_txt + pad_x * 2
        box_h = alto_txt + pad_y * 2
        box_x = (ancho_total - box_w) / 2
        draw.rounded_rectangle(
            [box_x, y, box_x + box_w, y + box_h],
            radius=corner_radius, fill=box_color,
        )
        # bbox[0]/bbox[1] son el offset que deja el propio glyph (ascenders,
        # side-bearing) — hay que restarlo para que el texto quede centrado
        # de verdad dentro de la caja.
        draw.text((box_x + pad_x - bbox[0], y + pad_y - bbox[1]), linea, font=font, fill=text_color)
        y += box_h + line_gap

    img.save(out_path)


def generate_variant_gancho(
    input_path, output_path, gancho_texto,
    duracion_gancho=3.0, font_size=54, y_pos_pct=0.42,
    crf=20, preset="veryfast", max_width=720,
):
    overlay_path = f"{output_path}.overlay.png"
    build_caption_overlay(gancho_texto, overlay_path, font_size=font_size)

    # Downscale primero, igual que en build_filter_chain() para /procesar:
    # con videos reales (ej. 1080x1920 de celular) sin este downscale, escalar +
    # componer la imagen encima pica de memoria y Railway mata el proceso (OOM).
    filter_complex = (
        f"[0:v]scale='min({max_width},iw)':'-2'[base];"
        f"[base][1:v]overlay=(W-w)/2:H*{y_pos_pct}:enable='between(t,0,{duracion_gancho})'"
    )

    cmd = [
        "ffmpeg", "-y",
        "-threads", "1",  # limita picos de memoria en contenedores chicos (Railway starter)
        "-i", input_path,
        "-i", overlay_path,
        "-filter_complex", filter_complex,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-c:a", "copy",  # el audio no cambia, solo el overlay visual
        output_path,
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_detail = result.stderr.decode(errors="ignore")[-800:]
    if result.returncode != 0:
        err_detail = f"[returncode={result.returncode}] {err_detail}"

    try:
        os.remove(overlay_path)
    except OSError:
        pass

    return result.returncode == 0, err_detail


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def download_video(url, dest_path):
    session = requests.Session()
    r = session.get(url, stream=True, timeout=60)
    r.raise_for_status()

    content_type = r.headers.get("Content-Type", "")

    # Google Drive: si el archivo es grande, en vez del archivo devuelve una
    # página HTML de confirmación ("no se puede escanear por virus"). Hay que
    # reenviar la request con el confirm token que viene en esa página/cookies.
    if "text/html" in content_type and "drive.google.com" in url:
        confirm_token = None
        for key, value in r.cookies.items():
            if key.startswith("download_warning"):
                confirm_token = value
                break

        if not confirm_token:
            # Buscarlo en el body de la respuesta (formato alternativo de Drive)
            import re
            match = re.search(r"confirm=([0-9A-Za-z_]+)", r.text)
            if match:
                confirm_token = match.group(1)

        if confirm_token:
            r = session.get(url, params={"confirm": confirm_token}, stream=True, timeout=60)
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "")

    if "text/html" in content_type:
        raise ValueError(
            "La URL devolvió una página HTML en vez de un video. "
            "Verificá que el archivo esté compartido como 'Cualquiera con el enlace' "
            "o usá un hosting sin verificación intermedia."
        )

    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

    if dest_path.stat().st_size < 20000:
        raise ValueError(
            f"El archivo descargado es sospechosamente chico ({dest_path.stat().st_size} bytes) "
            "— probablemente no es el video real."
        )


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
            print(f"[job {job_id}] error en variante {i}: {err}", flush=True)
            jobs[job_id].setdefault("errores_variantes", []).append(
                {"variante": i, "detalle": err}
            )

    jobs[job_id]["estado"] = "listo"
    jobs[job_id]["variantes"] = variantes
    notify_callback(job_id, {"estado": "listo", "variantes": variantes})


def run_job_ganchos(job_id, video_url, ganchos, duracion, font_size, crf, preset):
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

    for i, gancho_texto in enumerate(ganchos, start=1):
        out_name = f"gancho_{i}_{slugify(gancho_texto)}_{uuid.uuid4().hex[:6]}.mp4"
        out_path = job_dir / out_name

        ok, err = generate_variant_gancho(
            str(input_path), str(out_path), gancho_texto,
            duracion_gancho=duracion, font_size=font_size, crf=crf, preset=preset,
        )

        if ok:
            md5 = file_md5(out_path)
            variante_info = {
                "archivo": out_name,
                "url": f"{PUBLIC_BASE_URL}/{job_id}/{out_name}" if PUBLIC_BASE_URL else str(out_path),
                "md5": md5,
                "gancho": gancho_texto,
            }
            variantes.append(variante_info)
            jobs[job_id]["variantes"] = variantes
            # Notificación incremental: cada gancho listo se reporta al toque
            notify_callback(job_id, {"estado": "parcial", "variante": variante_info})
        else:
            print(f"[job {job_id}] error en gancho {i} ('{gancho_texto}'): {err}", flush=True)
            jobs[job_id].setdefault("errores_variantes", []).append(
                {"gancho": gancho_texto, "detalle": err}
            )
            # Sin este aviso, la fila de esa variante se queda en "procesando"
            # para siempre en la web (nunca recibe un callback que la mueva a
            # 'lista' ni a 'error').
            notify_callback(job_id, {"estado": "error_variante", "gancho": gancho_texto, "error": err})

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


@app.route("/ganchos", methods=["POST"])
def ganchos_endpoint():
    if not check_auth(request):
        return jsonify({"error": "no autorizado"}), 401

    data = request.get_json(force=True, silent=True) or {}
    video_url = data.get("video_url")
    ganchos = data.get("ganchos")
    duracion = float(data.get("duracion", 3.0))
    font_size = int(data.get("font_size", 42))
    crf = int(data.get("crf", 20))
    preset = data.get("preset", "veryfast")

    if not video_url:
        return jsonify({"error": "falta video_url"}), 400
    if not ganchos or not isinstance(ganchos, list) or not all(isinstance(g, str) and g.strip() for g in ganchos):
        return jsonify({"error": "falta ganchos (lista de textos no vacía)"}), 400

    job_id = uuid.uuid4().hex
    jobs[job_id] = {"estado": "en_cola", "variantes": [], "error": None}

    thread = threading.Thread(
        target=run_job_ganchos,
        args=(job_id, video_url, ganchos, duracion, font_size, crf, preset),
        daemon=True,
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
