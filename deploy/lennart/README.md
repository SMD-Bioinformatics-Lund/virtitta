# Lennart deployment

This directory contains the Docker deployment for the legacy Lennart host.

## Initial setup

The transfer directory on Lennart is `/data/bnf/dev/jonas/hcv/virtitta-docker`. The tracked `.env` contains Lennart's host paths and container UID/GID; review it before the first deployment:

```sh
$EDITOR .env
```

Set `VIRTITTA_DATA` to an absolute persistent-data path writable by `VIRTITTA_UID:VIRTITTA_GID`. Add `apache.conf` to the existing TLS virtual host and reload Apache. Install `virtitta.service` under `/etc/systemd/system/`, then run `systemctl daemon-reload` and enable it.

## Deploy a build

On the development machine:

```sh
./deploy/lennart/build-image
```

This builds a versioned image and copies the deployment bundle to Lennart. On Lennart:

```sh
cd /data/bnf/dev/jonas/hcv/virtitta-docker
./deploy-image
```

`deploy-image` loads the archive, records its full tag in `.env`, and copies only the live Compose files to `/var/www/virtitta`. It restarts `virtitta.service` when installed, otherwise it runs `docker-compose up -d`.

## Operations

```sh
sudo systemctl status virtitta
sudo systemctl restart virtitta
docker-compose logs --tail=200 virtitta
./import-run --run-dir /access/virpipa/hcv/RUN_NAME
```

Automation uses the root-owned `sbin-virtitta-import` installed as `/usr/local/sbin/virtitta-import`, together with its restricted sudoers rule.
