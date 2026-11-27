FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
# Debian's security fixes newer than the base image (libpam CVE-2025-6020), until the digest moves.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
# kopf names itself from the user; the image's user has no passwd entry.
ENV USER=db-logins HOME=/tmp PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["db-logins"]
