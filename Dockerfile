FROM python:3.11-slim

# FFmpeg + fuente DejaVu de respaldo + fuentes libres tipo Helvetica para el
# texto de los ganchos (ver nota abajo).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-urw-base35 fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

# Helvetica es una fuente comercial (Monotype/Linotype) que no se puede
# distribuir dentro de la imagen. Usamos Nimbus Sans Bold (URW Base35), el
# clon libre metric-compatible de Helvetica que trae Ghostscript/Linux desde
# hace décadas — mismas formas y proporciones, sin problema de licencia. Si por
# lo que sea el paquete no trae ese archivo, cae a Liberation Sans Bold (el
# equivalente libre de Arial, visualmente casi idéntico a Helvetica también).
RUN mkdir -p /usr/share/fonts/truetype/helvetica && \
    ( f=$(find /usr/share/fonts -iname "NimbusSans-Bold*" | head -1); [ -n "$f" ] && cp "$f" /usr/share/fonts/truetype/helvetica/Helvetica-Bold.ttf ) ; \
    if [ ! -f /usr/share/fonts/truetype/helvetica/Helvetica-Bold.ttf ]; then \
      f=$(find /usr/share/fonts -iname "LiberationSans-Bold*" | head -1); cp "$f" /usr/share/fonts/truetype/helvetica/Helvetica-Bold.ttf; \
    fi

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Railway inyecta la variable PORT automĂĄticamente
CMD ["python3", "app.py"]
