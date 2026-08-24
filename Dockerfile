FROM python:3.11-slim

# FFmpeg (no viene incluido en la imagen base de Python)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Railway inyecta la variable PORT automĂĄticamente
CMD ["python3", "app.py"]
