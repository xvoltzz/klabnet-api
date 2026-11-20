FROM python:3.13-slim

WORKDIR /srv

# ffmpeg makes uploaded videos into HLS (routers/videos.py). Debian's has
# NVENC/NVDEC built in and loads the NVIDIA driver's libraries at run time,
# so the same image encodes on the GPU when compose.gpu.yml hands it one
# (app/encoder.py, VIDEO_ENCODER) and on the CPU otherwise.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8765

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]
