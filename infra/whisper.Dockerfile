FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin whisper
WORKDIR /app
COPY infra/whisper_requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/whisper_service.py ./whisper_service.py
RUN mkdir /models && chown whisper:whisper /models
USER whisper
EXPOSE 8080
CMD ["uvicorn", "whisper_service:app", "--host", "0.0.0.0", "--port", "8080"]
