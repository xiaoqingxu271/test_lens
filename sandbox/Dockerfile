# Default TestLens sandbox image.
#
# The production image should be pinned by digest through PYTHON_IMAGE, e.g.
#   docker build --build-arg PYTHON_IMAGE=python:3.12-slim@sha256:<digest> ...
# CI builds this image and runs the container test suite inside it.
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m pip install --no-cache-dir "pytest==9.2.0"

WORKDIR /project
