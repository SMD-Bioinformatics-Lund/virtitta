FROM mambaorg/micromamba:2.8.1

# Build this image on a modern Docker host, then transfer it to Lennart with
# docker save/load. Numeric ownership keeps the image compatible with legacy
# image metadata; the base image's mambauser account is UID/GID 57439.
COPY --chown=57439:57439 environment.container.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml \
    && micromamba clean --all --yes

WORKDIR /app
COPY --chown=57439:57439 pyproject.toml README.md LICENSE ./
COPY --chown=57439:57439 virtitta ./virtitta

USER root
RUN mkdir -p /config/data && chown -R 57439:57439 /config
USER 57439:57439

ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "-m", "virtitta.cli"]
CMD ["serve", "--config", "/config/virtitta.toml", "--host", "0.0.0.0", "--port", "8000"]
