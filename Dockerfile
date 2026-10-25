# bandcamp-sync image: the orchestrator plus the tools it shells out to.
#
#   docker build --target runtime -t bandarr .     # runtime image (docker compose does this)
#   docker build --target test -t bandarr-test .   # same image + pytest; `docker run --rm bandarr-test`
# Always name the target: without --target Docker builds the LAST stage, which is `test`.
#
# Everything third-party is pinned: base images by tag (Debian bookworm everywhere, so the
# Rust binary matches the runtime glibc), flaccheck by git commit with its Cargo.lock
# (--locked), lossless-verify by git commit in requirements.txt, ffmpeg as a static build.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG RUST_IMAGE=rust:1.99.1-bookworm
ARG FFMPEG_IMAGE=mwader/static-ffmpeg:7.1


# --- flaccheck: second detector of the anti-transcode gate (Rust, MIT) -------------------------
FROM ${RUST_IMAGE} AS flaccheck
ARG FLACCHECK_COMMIT=f9b011ce80512d8e23f106e0abd28b8d3f8580ce
RUN git clone --quiet https://github.com/dasunNimantha/flaccheck.git /src \
 && cd /src && git checkout --quiet "${FLACCHECK_COMMIT}" \
 && cargo build --release --locked -p flaccheck


# --- ffmpeg / ffprobe: static binaries (libmp3lame, flac, aiff... included) --------------------
FROM ${FFMPEG_IMAGE} AS ffmpeg


# --- Python wheels, built where git is available so the runtime image does not need it ---------
FROM ${PYTHON_IMAGE} AS wheels
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /requirements.txt
RUN pip wheel --no-cache-dir -r /requirements.txt -w /wheels


# --- runtime -------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=flaccheck /src/target/release/flaccheck /usr/local/bin/flaccheck
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index /wheels/*.whl

# uid/gid 1000 like the slskd container, so both share ./data without permission games
RUN groupadd --gid 1000 bandarr && useradd --uid 1000 --gid 1000 --no-create-home bandarr
WORKDIR /app
COPY bandarr /app/bandarr
COPY tools /app/tools
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLACCHECK=/usr/local/bin/flaccheck \
    DATA_DIR=/data
USER 1000:1000
ENTRYPOINT ["python", "-m", "bandarr.sync"]


# --- test: the real image with pytest on top (real ffmpeg, flaccheck and lossless-verify) -------
FROM runtime AS test
USER root
RUN pip install --no-cache-dir pytest
COPY tests /app/tests
USER 1000:1000
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/app/tests"]
