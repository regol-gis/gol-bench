FROM php@sha256:5d55c49594dd833e2e4ff677b65f7d8ff48817f443a60691d9b2b382413bba42
RUN apt-get update && apt-get install -y --no-install-recommends g++ make cmake libffi-dev \
    && docker-php-ext-install ffi \
    && rm -rf /var/lib/apt/lists/*
