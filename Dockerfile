FROM python:3.11-slim

# FFmpeg + fuente DejaVu (usada por el filtro drawtext de los ganchos de texto)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Railway inyecta la variable PORT automĂĄticamente
CMD ["python3", "app.py"]
