FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

ARG PIP_VERSION=26.1.1
ARG SETUPTOOLS_VERSION=82.0.1
ARG WHEEL_VERSION=0.47.0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
RUN python -m pip install \
        "pip==${PIP_VERSION}" \
        "setuptools==${SETUPTOOLS_VERSION}" \
        "wheel==${WHEEL_VERSION}" \
    && pip install -r requirements.txt

COPY . .
RUN pip install --no-build-isolation -e .

EXPOSE 5678 8080

CMD ["python", "etf_web.py"]
