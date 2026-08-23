FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000
# Shell form on purpose: the exec form (a JSON array) does not expand variables,
# so ${PORT} would be passed to uvicorn as a literal string. Render, Railway and
# Heroku all inject the port they expect the app to bind; ignoring it leaves the
# platform re-detecting the port and restarting the deploy on every release.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
