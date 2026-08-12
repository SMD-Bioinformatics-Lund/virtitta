# Lennart deployment

This directory contains the Docker deployment for the legacy Lennart host.

## Initial setup

Copy these files to `/data/bnf/dev/jonas/hcv/virtitta-docker`, then configure the host paths and container UID/GID:

```sh
cp .env.example .env
mkdir -p data
$EDITOR .env
docker-compose config
```

The directory selected by `VIRTITTA_DATA` must be writable by `VIRTITTA_UID:VIRTITTA_GID`. Add `apache.conf` to the existing TLS virtual host and reload Apache.

## Deploy a build

On the development machine:

```sh
./deploy/lennart/build-image
```

This builds a versioned image and copies the deployment files to Lennart. On Lennart:

```sh
cd /data/bnf/dev/jonas/hcv/virtitta-docker
./deploy-image
```

`deploy-image` loads the archive, records its full tag in `.env`, and recreates the service. If systemd owns the Compose lifecycle, replace its final `docker-compose up -d` action with a restart of `virtitta.service`.

## Operations

```sh
docker-compose ps
docker-compose logs --tail=200 virtitta
docker-compose restart virtitta
./import-run --run-dir /access/virpipa/hcv/RUN_NAME
```

Automation uses the root-owned `sbin-virtitta-import` installed as `/usr/local/sbin/virtitta-import`, together with its restricted sudoers rule.
