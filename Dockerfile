FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* && mkdir /data
ENV PYTHONUNBUFFERED=1 STATE_PATH=/data/state.json
WORKDIR /app
COPY ovh_watch.py .
ENTRYPOINT ["python", "/app/ovh_watch.py"]
