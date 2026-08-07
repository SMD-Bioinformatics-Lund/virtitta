FROM mambaorg/micromamba:2.8.1

COPY --chown=$MAMBA_USER:$MAMBA_USER environment.container.yml /tmp/environment.yml
RUN micromamba install --yes --name base --file /tmp/environment.yml \
    && micromamba clean --all --yes

WORKDIR /app
COPY --chown=$MAMBA_USER:$MAMBA_USER pyproject.toml README.md LICENSE ./
COPY --chown=$MAMBA_USER:$MAMBA_USER virtitta ./virtitta

USER root
RUN mkdir -p /config/data && chown -R $MAMBA_USER:$MAMBA_USER /config
USER $MAMBA_USER

ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["/usr/local/bin/_entrypoint.sh", "python", "-m", "virtitta.cli"]
CMD ["serve", "--config", "/config/virtitta.toml", "--host", "0.0.0.0", "--port", "8000"]
