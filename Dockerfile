FROM denoland/deno:debian
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/bluenote-venv
ENV PATH="/opt/bluenote-venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PORT=10000
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY index.html servidor_online.py /app/
EXPOSE 10000
ENTRYPOINT ["/opt/bluenote-venv/bin/python"]
CMD ["servidor_online.py"]
