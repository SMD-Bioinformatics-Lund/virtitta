# Virtitta Admin Guide

This guide covers setup, command-line operations, authentication, and deployment notes.

## Environment

Local development currently assumes the `virtitta` conda environment:

```bash
source ~/miniforge3/etc/profile.d/conda.sh
conda activate virtitta
cd ~/git/virtitta
```

Use `PYTHONPATH=$PWD python -m virtitta.cli ...` from the repository checkout, or install the package and use the
`virtitta` console script.

For a fresh checkout, install the Python package into the active environment:

```bash
pip install -e .
```

## Configuration

Runtime configuration is read from `virtitta.toml`.

Important sections:

- `[app]`: title, bind address, port, and optional reverse-proxy `root_path`
- `[database]`: SQLite database path
- `[results_roots]`: Linux and Windows-visible VirPipa result roots
- `[exports]`: server-side LIMS export root
- `[cache]`: local cache for small imported artifacts
- `[auth]`: optional local login and session settings
- `[webigv]`: optional browser IGV fallback settings
- `[cluster]`: optional selected-sample clustering settings
- `[annotations]`: assignable sample categories and optional restricted categories
- `[ui]`: table columns, defaults, labels, width caps, and highlight rules

Start from `virtitta.example.toml` for new deployments.

Sample categories are configured in `[annotations]`:

```toml
[annotations]
sample_categories = ["production", "validation", "EQA", "test"]
restricted_sample_categories = ["test"]
```

Restricted categories are hidden from roles without category-update permission. With the default roles, `admin` and
`reviewer` can see and assign `test`, while `commenter` and `viewer` do not see those samples in the table, detail
pages, direct file/export routes, or category filters.

## Result Roots And Stored Paths

Each configured result root has three parts:

```toml
[[results_roots]]
name = "hcv_test_results"
linux_path = "/fs1/jonas/hcv/test_results"
windows_path = "Q:/jonas/hcv/test_results"
```

- `name` is a stable logical label. Virtitta stores this in SQLite as `runs.source_root_name` and
  `samples.source_root_name`.
- `linux_path` is the server-side path Virtitta reads for import, downloads, webIGV, cache refresh, and clustering.
- `windows_path` is used only when building desktop IGV URLs for clients that see the files through a Windows mapping.

Sample rows also store `sample_results_relpath`, the path from the configured root to the directory containing that
sample's QC summary. Runtime file paths are resolved as:

```text
<results_roots[source_root_name].linux_path>/<sample_results_relpath>/<output path from QC JSON>
```

Desktop IGV uses the same relative path but starts from `windows_path`.

The import CLI chooses `source_root_name` by checking which configured `linux_path` contains `--run-dir`. If more than
one root matches, the most specific root path wins. This allows old and new storage trees to coexist, for example one
root for `/fs1/jonas/hcv/test_results` and another for `/access/hcv`.

To move a whole result tree without changing relative paths, keep the same root `name` and update only `linux_path` and
`windows_path` in `virtitta.toml`. If you also rename the root, existing SQLite rows must be reimported or updated from
the old `source_root_name` to the new one.

## IGV Configuration

Desktop IGV remains the preferred workflow for routine review:

```toml
[igv]
enabled = true
base_url = "http://localhost:60151/load"
```

Desktop IGV URLs are built from `results_roots[].windows_path`.

Enable webIGV only when the Virtitta server can read the result roots through mounted Linux paths:

```toml
[webigv]
enabled = true
igv_js_url = "/static/igv.min.js"
```

webIGV serves track files through authenticated Virtitta routes using `results_roots[].linux_path`. The mounted result
root should be read-only for the Virtitta process. Index sidecars may be explicit in the VirPipa QC JSON or inferred
from standard filenames: `<fasta>.fai`, `<cram>.crai`, and `<vcf>.csi`. Import and refresh fail if a required sidecar is
missing for a reported FASTA, CRAM, or VCF output.

webIGV disables IGV.js' browser-side CRAM slice MD5 check. VirPipa writes CRAMs against the sample FASTA, and Virtitta
serves the matching FASTA, FAI, CRAM, and CRAI files from the imported QC JSON. In practice IGV.js can still ask the
browser genome object for an empty reference slice during CRAM decoding, which produces a false mismatch with calculated
MD5 `d41d8cd98f00b204e9800998ecf8427e`. Use the imported files or samtools if you need to audit CRAM/reference
integrity outside the browser.

## Cluster Configuration

Selected-sample clustering is disabled by default. Enable it only when the Virtitta server can execute `mafft` and
`iqtree3` from the configured commands:

```toml
[cluster]
enabled = true
output_root = "data/clusters"
grapetree_url = "https://mtlucmds1.lund.skane.se/grapetree/"
public_base_url = "https://virtitta.example.org"
max_concurrent_jobs = 1
timeout_seconds = 3600
input_output_key = "iupac_fasta"
five_prime_trim = 50
poly_t = true
poly_t_min_length = 10
poly_t_seed_length = 12
poly_t_seed_min_t = 10
poly_t_max_trailing_bases = 100
mafft_command = "mafft"
samtools_command = "samtools"
iqtree_command = "iqtree3"
mafft_args = ["--auto"]
iqtree_threads = 4
iqtree_args = []
```

For service users or `micromamba run` deployments, keep `mafft_command` and `iqtree_command` as command names when the
runtime environment reliably adds the tool directory to `PATH`. Use absolute paths only in host-specific configs where
the service account cannot otherwise resolve the tools.

For the local conda workflow, install the command-line tools into the same environment that runs Virtitta:

```bash
micromamba install -c conda-forge -c bioconda mafft iqtree samtools
```

V1 requires at least three selected samples, uses the imported `iupac_fasta` output for each selected sample, derives
tree IDs from Virtitta sample metadata, trims the configured number of 5' bases, trims a poly-T tail only when at most
`poly_t_max_trailing_bases` follow the T-rich seed. Exact T runs of at least `poly_t_min_length` are trimmed, and fuzzy
tails are trimmed when a `poly_t_seed_length` window contains at least `poly_t_seed_min_t` T bases. The prepared FASTA
is aligned with MAFFT, then IQ-TREE 3 runs with `-T` set from `cluster.iqtree_threads`. Cluster artifacts are written
under `cluster.output_root`;
queued or running jobs are marked failed on server restart. `cluster.log` includes a `prepare-fasta summary` showing
whether 5' and poly-T trimming ran, how many records and bases were trimmed, one line per poly-T trim event, and
external command exit codes and elapsed times.

The GrapeTree handoff uses a `tree=` URL parameter with a tokenized public link to `grapetree.json`. That JSON embeds
the completed Newick tree and metadata table, avoiding the stricter separate metadata URL loader in some GrapeTree
deployments. Tokenized public artifact routes expose the completed tree, metadata, and GrapeTree JSON files only.

Set `cluster.public_base_url` on deployed servers when the request host seen by Virtitta is not reachable by the
GrapeTree server. For example, if Virtitta runs behind a tunnel or reverse proxy, this must be the external Virtitta
origin, not `http://127.0.0.1:8000`.

## Core Commands

Initialize or migrate the SQLite schema:

```bash
python -m virtitta.cli init-db --config virtitta.toml
```

Run the development server:

```bash
python -m virtitta.cli serve --config virtitta.toml
```

Override host and port:

```bash
python -m virtitta.cli serve --config virtitta.toml --host 0.0.0.0 --port 8001
```

## Import Commands

Import one completed VirPipa run:

```bash
python -m virtitta.cli import-run --config virtitta.toml --run-dir /path/to/results/<run_name>
```

`import-run` accepts both flat sample directories (`<run>/<sample>/<sample>_qc_summary.json`) and the legacy nested
layout (`<run>/<sample>/results/<sample>_qc_summary.json`). Result file paths in QC JSON are resolved relative to the
directory containing the QC summary.

If restored runs need external Clarity metadata:

```bash
python -m virtitta.cli import-run \
  --config virtitta.toml \
  --run-dir /path/to/results/<run_name> \
  --clarity-sample-info /path/to/clarity_sample_info.json
```

When QC summaries are missing CT, library concentration, library fragment length, department, classification,
sequencing runs, or sample submission signing, `import-run` looks for Clarity metadata in this order:

1. `--clarity-sample-info /path/to/clarity_sample_info.json`
2. `<run_dir>/pipeline_info/clarity_sample_info.json`
3. `<run_dir>/clarity_sample_info.json`
4. `<imports.clarity_metadata_root>/*_<YYMMDD>_<instrument>_<flowcell>.json`, when configured. This matches
   Clarity files such as `NovaSeqX_260601_A01932_AHFNTGDMX2.json` to a run directory such as
   `260601_A01932_0123_AHFNTGDMX2`, where the Clarity filename omits the instrument run number.
   If the run directory name contains `+` because runs were concatenated, Virtitta matches the configured-root
   Clarity file using the latest dated run segment only.

Values already present in the QC summaries are kept. Missing or incomplete numeric Clarity metadata does not block
import, but the CLI and run-refresh action warn so the operator can locate the file and re-run the import.

Import all runs under configured result roots:

```bash
python -m virtitta.cli import-root --config virtitta.toml
```

Add a manually failed sample when no QC summary exists:

```bash
python -m virtitta.cli import-sample \
  --config virtitta.toml \
  --sample-id SAMPLE123 \
  --lid LID123 \
  --ct 31.2 \
  --library-concentration 1.7
```

If `--run-dir` is omitted, the sample is stored under `manual_failed_samples`.

## Maintenance Commands

Backfill flattened AF count columns from stored raw JSON:

```bash
python -m virtitta.cli backfill-af-counts --config virtitta.toml
```

Verify cached artifacts for one sample:

```bash
python -m virtitta.cli verify-cache --config virtitta.toml --sample-run-id SAMPLE001_run_name
```

Verify all samples in one run:

```bash
python -m virtitta.cli verify-cache --config virtitta.toml --run-name run_name
```

Verify all imported runs except `manual_failed_samples`:

```bash
python -m virtitta.cli verify-cache --config virtitta.toml --all-runs
```

Refresh/rebuild cache entries before verification:

```bash
python -m virtitta.cli verify-cache --config virtitta.toml --all-runs --refresh
```

Interactive artifact reads compare the current result-root path, size, and nanosecond mtime with the cache record. A
changed source is copied into place atomically before it is served. If the result root is unavailable, an existing
cached copy remains available but responses identify it as `cache-offline-unverified` and include an HTTP warning.
`verify-cache` additionally hashes both files and remains useful as an explicit integrity audit; it is not required as
a scheduled freshness mechanism. Clustering and distance jobs always use live result-root inputs rather than this
general UI/export cache.

## Authentication

Authentication is optional and disabled by default.

Enable local authentication:

```toml
[auth]
enabled = true
provider = "local"
session_days = 7
cookie_name = "virtitta_session"
cookie_secure = true
pbkdf2_iterations = 600000
```

Use `cookie_secure = true` when Virtitta is served over HTTPS. For temporary local HTTP testing, use `false`.

Local passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. Active sessions use opaque random tokens stored in
HttpOnly SameSite cookies. Authenticated POST forms use CSRF tokens.

`/help` is intentionally public. Anonymous visitors see viewer-level documentation without sample data or
administrative instructions. After login, the same page adds help for controls permitted to the current role. This
public route does not make the corresponding application actions public; their existing route permissions remain in
effect.

## User Management

Create users:

```bash
python -m virtitta.cli create-user --config virtitta.toml --username alice --role admin
python -m virtitta.cli create-user --config virtitta.toml --username bob --role reviewer
```

List users:

```bash
python -m virtitta.cli list-users --config virtitta.toml
```

Change a role:

```bash
python -m virtitta.cli set-user-role --config virtitta.toml --username bob --role viewer
```

Reset a password:

```bash
python -m virtitta.cli reset-user-password --config virtitta.toml --username bob
```

Disable or enable a user:

```bash
python -m virtitta.cli disable-user --config virtitta.toml --username bob
python -m virtitta.cli enable-user --config virtitta.toml --username bob
```

Disabling a user clears active sessions for that user.

## Roles

- `admin`: all actions, including deletes, metadata overrides, run refresh, and CLI user administration
- `reviewer`: QC, categories, groups, comments, own comment deletion, read exports, and server-side LIMS export
- `commenter`: view, groups, read exports, add comments, and delete own comments
- `viewer`: view and read exports only

## Deployment Notes

Virtitta has one container image for both direct local use and HTTPS reverse-proxy deployment. The image contains
Python, Virtitta, MAFFT, and IQ-TREE. Configuration, persistent application data, result roots, and optional metadata
roots remain outside the image.

### Standalone Docker Service

Copy `.env.example` to `.env` and set the host paths. The container paths in `.env` must match the corresponding
`linux_path` and `clarity_metadata_root` values in the mounted TOML file. For example:

```toml
[app]
root_path = ""

[imports]
clarity_metadata_root = "/clarity"

[[results_roots]]
name = "hcv_results"
linux_path = "/results"
windows_path = "Q:/virpipa/hcv"
```

Keep authentication cookies non-secure only for direct HTTP testing. Create the persistent data directory, then build
and start the service:

```bash
mkdir -p data
docker compose build
docker compose up -d
docker compose logs -f virtitta
```

The examples use the current `docker compose` plugin. On a host that uses legacy Compose, substitute `docker-compose`;
the automated import wrapper detects either command.

The defaults expose Virtitta only at `http://127.0.0.1:8000/`. Set `VIRTITTA_LISTEN_ADDRESS=0.0.0.0` only when direct
LAN access is intentional, and enable authentication before doing so. Run administrative commands as one-shot
containers so they use the same image and mounts as the web service:

```bash
docker compose run --rm --no-deps virtitta init-db --config /config/virtitta.toml
docker compose run --rm --no-deps virtitta create-user --config /config/virtitta.toml --username USER --role reviewer
docker compose run --rm --no-deps virtitta import-run --config /config/virtitta.toml --run-dir /results/RUN
```

Set `VIRTITTA_UID` and `VIRTITTA_GID` to the owner of the host data directory (`id -u` and `id -g` for the current
user). Compose runs the container as that non-root identity so it can update SQLite and the mounted cache, cluster, and
export directories without broadening their permissions. The same identity must be able to read all mounted
configuration, result, and metadata paths.

### Apache HTTPS Deployment Under `/virtitta`

Use the same image with server-specific `.env` values. The current server deployment should publish only to loopback:

```dotenv
VIRTITTA_LISTEN_ADDRESS=127.0.0.1
VIRTITTA_PORT=5812
VIRTITTA_CONFIG=/path/to/virtitta.toml
VIRTITTA_DATA=/path/to/virtitta-data
VIRTITTA_RESULTS_ROOT=/access/virpipa/hcv
VIRTITTA_RESULTS_CONTAINER_ROOT=/access/virpipa/hcv
VIRTITTA_CLARITY_ROOT=/fs2/seqdata/clarity/done
VIRTITTA_CLARITY_CONTAINER_ROOT=/fs2/seqdata/clarity/done
VIRTITTA_FORWARDED_ALLOW_IPS=*
```

The deployed TOML settings must include:

```toml
[app]
root_path = "/virtitta"

[cluster]
public_base_url = "https://mtlucmds1.lund.skane.se/virtitta"

[auth]
enabled = true
cookie_secure = true
```

`deploy/apache-virtitta.conf` contains the proxy directives to place in the existing TLS-enabled Apache configuration.
Confirm that `proxy`, `proxy_http`, and `headers` are enabled and run `apache2ctl configtest` before reloading Apache.
The `ProxyPass` target preserves `/virtitta`; this must match `app.root_path` so mounted static files and generated
public URLs use the same prefix.

`VIRTITTA_FORWARDED_ALLOW_IPS=*` is appropriate here only because the published backend port is restricted to host
loopback and Apache replaces the forwarded scheme. Do not combine this setting with an externally published backend
port.

### Automated Imports

The `.sqlimport` file format and its `.running`, `.done`, and `.error` lifecycle do not change. Configure the existing
runner to call `deploy/virtitta-import`; it appends the marker contents as before:

```json
"command": "/data/bnf/dev/jonas/hcv/virtitta/deploy/virtitta-import"
```

The wrapper invokes `docker compose run --rm --no-deps` with the same image and mounts as the web service, forwards all
arguments unchanged, and returns Virtitta's exit status. Marker validation, reporting, retries, and suffix changes stay
with the existing runner.

### Persistent Data And Updates

Back up the directory configured by `VIRTITTA_DATA`; it contains the SQLite database, cached outputs, clustering
artifacts, and LIMS exports when the supplied relative paths are used. Rebuild and recreate the service after an
application update:

```bash
docker compose build
docker compose up -d
```

Keep TLS termination in Apache. LDAP/AD remains a possible future authentication provider while Virtitta permissions
remain internal.

### Lennart Legacy Deployment Directory

The modern `Dockerfile` and `compose.yaml` remain the defaults for current Docker installations. Lennart runs Docker
18.09 with a seccomp profile that rejects a syscall used by current Micromamba. Its builder also does not expand
`$MAMBA_USER` in `COPY --chown`. Build the separate Lennart image on a modern Docker host and transfer the resulting
archive instead of building it on Lennart:

```bash
./deploy/lennart/build-image
```

The build and tracked Lennart `.env` are transferred to `/data/bnf/dev/jonas/hcv/virtitta-docker`. Its persistent
data directory is `/data/bnf/appdata/virtitta`:

```bash
cd /data/bnf/dev/jonas/hcv/virtitta-docker
```

The configured `VIRTITTA_UID` and `VIRTITTA_GID` must match the owner that manages the data:

```bash
id -u
id -g
sudo chown -R 1009:1004 /data/bnf/appdata/virtitta
```

Load and deploy the transferred image:

```bash
./deploy-image
docker-compose logs -f virtitta
```

Install `virtitta.service` under `/etc/systemd/system/` before the first deployment. Its working directory is
`/var/www/virtitta`; systemd owns subsequent service start, stop, and restart operations. Without the unit,
`deploy-image` falls back to `docker-compose up -d`.

`build-image` writes `virtitta-image.tar.gz` plus `virtitta-image.tag`. The deployment script validates the companion
tag, loads the archive, updates only `VIRTITTA_IMAGE` in the staging `.env`, and copies `docker-compose.yml`, `.env`,
`virtitta.toml`, `import-run`, and `run-command` to `/var/www/virtitta`. It then restarts the service. Consequently,
`docker ps` shows the full version tag. Each archive also contains the movable `virtitta:lennart` tag. To pin or roll
back to any loaded version, set it in `/var/www/virtitta/.env` and run `sudo systemctl restart virtitta`:

```dotenv
VIRTITTA_IMAGE=virtitta:v0.8.0-6-g9e478e2
```

Change it back to `virtitta:lennart` to follow subsequently loaded Lennart builds. `VIRTITTA_IMAGE_VERSION` may be set
when running `build-image` to override the version derived from `git describe --tags --always --dirty`.

Use `./import-run --run-dir /access/virpipa/hcv/RUN` for manual imports and configure the existing `.sqlimport` runner
to call `/var/www/virtitta/import-run`. The matching Apache directives are in `apache.conf`.

The directory's `docker-compose.yml` applies `seccomp=unconfined` only to Virtitta. This is required because the old
profile returns `EPERM` for syscalls unknown to Docker 18.09, preventing Micromamba environment activation. The
service remains non-root, publishes only on host loopback, mounts configuration/results/metadata read-only, and
receives write access only to the configured Virtitta data directory. Remove this exception when the server is
replaced.
## Distance-matrix jobs

Pairwise distance matrices use the existing `[cluster]` settings, output root, background executor, trimming configuration, and MAFFT command. They are available when `cluster.enabled = true`; no separate CLI or database migration is required. Coverage is positional and requires ≥1× read depth in both samples. To avoid mixing coordinate systems from different refresh times, distance jobs resolve all FASTA, CRAM, index, and supplied BED inputs directly from the imported result-root paths rather than the general output cache. Virtitta prefers `outputs.coverage_1x_bed` from imported QC JSON. For historical runs it executes `cluster.samtools_command` (default `samtools`) against the main CRAM and FASTA, then atomically caches the derived BED and a FASTA/FAI/CRAM/CRAI stat manifest under `cache.outputs_root/coverage-1x`. Invalid supplied masks and failed derivations fail the job; FASTA letter case is not used as coverage evidence.

Each job stores canonical JSON, an authenticated `coverage-masks.bed` containing the trimmed masks actually used, and compact and detailed TSV matrices for both 15% IUPAC and majority consensus. Canonical JSON also records a deterministic, mode-specific UPGMA display order based on total event counts. Off-diagonal pairs with no bases compared remain zero-valued internally, use a documented maximum-plus-one penalty only for UPGMA ordering, and export as `-1` or “No bases compared” in the TSV matrices. Distance artifacts require normal Virtitta authentication and are never served by the public GrapeTree artifact route.
