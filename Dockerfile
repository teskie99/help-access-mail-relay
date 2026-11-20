FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user. Staged files live in /data and are temporary by design.
RUN useradd --system --uid 10001 relay && mkdir -p /data && chown relay /data
USER relay

EXPOSE 8000
CMD ["python", "-m", "app.server"]
