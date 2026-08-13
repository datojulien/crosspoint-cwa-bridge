FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md requirements.lock ./
COPY src ./src

RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --no-deps --no-build-isolation .

EXPOSE 8094 8095

CMD ["python", "-m", "crosspoint_cwa_bridge.app"]
