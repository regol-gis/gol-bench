FROM postgres@sha256:e8a07d2c7579b038938cdba6c539e580e5001caf9ea203d397b5cc83e76bd938
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git autoconf automake libtool pkg-config libxml2-dev libproj-dev \
    libgeos-dev libjson-c-dev libprotobuf-c-dev protobuf-c-compiler postgresql-server-dev-16 \
    postgresql-16-postgis-3 postgresql-16-postgis-3-scripts ca-certificates \
    && rm -rf /var/lib/apt/lists/*
