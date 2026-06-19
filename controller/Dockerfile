# Python control layer: FastAPI web UI on :8099, OSC bridge to the SC engine.
FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY atelier ./atelier
COPY web ./web

ENV ATELIER_DATA=/data \
    SC_HOST=supercollider \
    SC_PORT=57120 \
    CONTROLLER_PORT=57140 \
    SC_STREAM_URL=http://supercollider:8200

EXPOSE 8099 57140/udp
CMD ["uvicorn", "atelier.server:app", "--host", "0.0.0.0", "--port", "8099"]
