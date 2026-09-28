# The website and the scanner in one container (`dip-scanner serve`), for Fly.io: see docs/DEPLOY.md.
#
#   docker build -t dip-scanner .                                  # OpenAI (the default provider)
#   docker build -t dip-scanner --build-arg EXTRAS=anthropic .     # Anthropic Claude; "azure" for Azure AI Foundry
#
# The database lives in DATA_DIR=/data, a Fly volume. scanner.toml and feeds.toml come with the image (in /app, the
# working directory); a copy in /data is used instead (see "Changing the scanner's settings" in docs/DEPLOY.md).
FROM python:3.12-slim

# Optional extras for other language model providers, comma-separated: "anthropic", "azure" or "anthropic,azure".
ARG EXTRAS=""

# MALLOC_ARENA_MAX=2: each cycle fetches the feeds on 8 threads, and without it glibc's per-thread memory pools kept
# the process about 75 MB larger (measured: 212 MB against 137 MB after 18 cycles).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    DATA_DIR=/data

# The server runs as "app", not root. Fly.io gives the volume's mount point to the image's user (USER below).
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home --home-dir /home/app --shell /bin/bash app \
    && mkdir -p /data \
    && chown app:app /data

# The dependencies first, in their own layer: a change to the code doesn't install them again. The package itself is
# an empty stand-in here (pyproject.toml needs the folders and a README to build).
COPY pyproject.toml /tmp/deps/
RUN cd /tmp/deps \
    && mkdir -p dip_scanner/web \
    && touch README.md dip_scanner/__init__.py dip_scanner/web/__init__.py \
    && pip install "/tmp/deps[web${EXTRAS:+,$EXTRAS}]" \
    && pip uninstall --yes news-dip-scanner \
    && rm -rf /tmp/deps

# The package (with its templates and static files), installed into site-packages, owned by root.
COPY pyproject.toml README.md /tmp/src/
COPY dip_scanner /tmp/src/dip_scanner
RUN pip install --no-deps /tmp/src \
    && rm -rf /tmp/src \
    && dip-scanner --version

# The settings files the scanner reads from its working directory (also where `fly ssh console` starts).
WORKDIR /app
COPY scanner.toml feeds.toml ./

USER app
EXPOSE 8080
CMD ["dip-scanner", "serve", "--host", "0.0.0.0", "--port", "8080"]
