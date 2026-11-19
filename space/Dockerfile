# The lxask demo on Hugging Face Spaces: the program built from source at a pinned
# commit, the model files of a release (checked against its SHA256SUMS), and a Gradio
# page in front. Nothing here is specific to the Space but the page: the answers are
# the ones `lxask` gives on your own machine.

# ---- the program ----------------------------------------------------------------
FROM rust:1-bookworm AS build
# the commit to build; it must have `lxask --explain` (after 1.1.0)
ARG LXASK_REF=main
# portable like the release builds: llama.cpp is not built for this builder's own CPU
ARG TARGET_CPU=x86-64-v3
RUN apt-get update && apt-get install -y --no-install-recommends cmake libclang-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none https://github.com/arxhr007/lxask /src \
    && git -C /src checkout --detach "$LXASK_REF" \
    && git -C /src log -1 --format='building lxask at %h %s'
ENV CARGO_BUILD_JOBS=2 CMAKE_BUILD_PARALLEL_LEVEL=2
RUN RUSTFLAGS="-C target-cpu=$TARGET_CPU" cargo build --release --manifest-path /src/rust/Cargo.toml -p lxask \
    && cp /src/rust/target/release/lxask /lxask

# ---- the model, adapters, encoder and docs index of a release --------------------
FROM debian:bookworm-slim AS files
ARG LXASK_TAG=v1.1.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /files
RUN base="https://github.com/arxhr007/lxask/releases/download/$LXASK_TAG" \
    && curl -fsSL --retry 4 -o SHA256SUMS "$base/SHA256SUMS" \
    && for name in $(grep -oE ' (lxask-.*\.gguf|restrict-.*\.gguf|precision-.*\.gguf|embed\.gguf|docs\.sqlite)$' SHA256SUMS); do \
           curl -fsSL --retry 4 -o "$name" "$base/$name"; \
       done \
    && sha256sum -c --ignore-missing SHA256SUMS \
    && mv lxask-*.gguf model.gguf && mv restrict-*.gguf restrict.gguf && mv precision-*.gguf precision.gguf \
    && rm SHA256SUMS && ls -la

# ---- the Space -------------------------------------------------------------------
FROM python:3.12-slim-bookworm
RUN useradd -m -u 1000 user
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=build /lxask /usr/local/bin/lxask
COPY --from=files --chown=user /files /home/user/lxask
COPY --chown=user app.py /home/user/app/app.py
USER user
ENV HOME=/home/user LXASK_HOME=/home/user/lxask LXASK_SERVE=off \
    GRADIO_SERVER_NAME=0.0.0.0 GRADIO_SERVER_PORT=7860 GRADIO_ANALYTICS_ENABLED=False PYTHONUNBUFFERED=1
WORKDIR /home/user/app
RUN lxask --version && lxask --explain "ls -la" >/dev/null
EXPOSE 7860
CMD ["python", "app.py"]
