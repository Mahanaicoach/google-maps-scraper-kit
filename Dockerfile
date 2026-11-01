# The kit's own Google Maps engine (engine/serve.py) with Debian's Chromium, for `docker compose up -d`.
# Python standard library only: nothing is installed with pip. Without Docker the same engine runs
# with `python3 scripts/engine.py start`.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 gmskit

WORKDIR /kit
COPY engine/ engine/
# The volume in docker-compose.yml mounts on .engine/data and takes this folder's owner.
RUN mkdir -p .engine/data && chown -R gmskit:gmskit .engine

# Not root, so Chromium keeps its sandbox where the container allows one; where Docker's seccomp
# profile blocks it, the engine relaunches Chromium without (engine/browser.py).
USER gmskit
ENV CHROME=/usr/bin/chromium PYTHONUNBUFFERED=1
EXPOSE 8080
# 0.0.0.0 inside the container, so Docker can forward the port; docker-compose.yml publishes it on
# the host's 127.0.0.1 only, and the engine still refuses a Host or Origin that is not localhost.
CMD ["python", "engine/serve.py", "--host", "0.0.0.0", "--port", "8080"]
