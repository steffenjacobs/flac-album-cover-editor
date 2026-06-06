# Pin both the Python minor and the Debian codename so base-OS upgrades are
# deliberate (a bare python:3.x-slim floats the codename).
FROM python:3.13-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    MUSIC_ROOT=/music

WORKDIR /app

# Install deps first for better layer caching. Pillow/mutagen/requests ship
# pure-Python or manylinux wheels, so no system build tools are needed.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8765

CMD ["python", "server.py"]
