FROM python:3.12.13-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade "pip==26.2.1" \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && groupadd --gid 10001 mixmill \
    && useradd --uid 10001 --gid 10001 --no-create-home \
       --shell /usr/sbin/nologin mixmill

WORKDIR /srv
COPY --chown=10001:10001 app/ /srv/app/
# Portainer may run the image as a different numeric UID/GID so it can write to
# the NAS data directory. Keep the immutable application tree readable and its
# directories traversable for that runtime user.
RUN chmod -R a+rX /srv/app

ENV VIDEO_DIR=/videos DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
