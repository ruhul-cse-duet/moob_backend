FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /code

# Wheels cover everything in requirements.txt, so there is nothing to compile.
# Installing build-essential anyway would add ~300MB to every layer rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Nothing here needs root, and a container that cannot write to its own code is
# one less thing an exploit can do.
RUN useradd --create-home --uid 10001 app && chown -R app:app /code
USER app

# Render assigns the port at run time and routes to it; a hard-coded one only
# works by accident. Locally it falls back to 8000.
ENV PORT=8000
EXPOSE 8000

# Render's proxy has no fixed address, so the forwarded headers have to be
# accepted from anywhere the container can be reached - which, behind Render, is
# only that proxy. Set through the environment rather than as a `*` argument,
# which a shell would expand into a directory listing.
ENV FORWARDED_ALLOW_IPS=*

# Shell form on purpose - $PORT has to be expanded at start, and exec keeps
# uvicorn as PID 1 so a shutdown signal reaches it instead of the shell.
# socket_app, not app: it wraps FastAPI and adds /socket.io beside it.
# Serving `app` directly would leave live messaging with nothing to connect to.
CMD exec uvicorn app.main:socket_app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers
