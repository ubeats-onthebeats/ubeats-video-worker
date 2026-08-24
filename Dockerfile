FROM python:3.11-slim

# FFmpeg + curl (para bajar la fuente de los ganchos) + fuente DejaVu de respaldo
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl && \
    rm -rf /var/lib/apt/lists/*

# Poppins ExtraBold: la fuente redondeada/gruesa usada para el texto de los
# ganchos (se renderiza con Pillow y se superpone al video, ver build_caption_overlay()
# en app.py). Se baja directo del repo oficial de Google Fonts.
RUN mkdir -p /usr/share/fonts/truetype/poppins && \
    curl -fsSL -o /usr/share/fonts/truetype/poppins/Poppins-ExtraBold.ttf \
      https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/Poppins-ExtraBold.ttf

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Railway inyecta la variable PORT automĂĄticamente
CMD ["python3", "app.py"]
