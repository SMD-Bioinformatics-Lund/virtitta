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

## Configuration

Runtime configuration is read from `virtitta.toml`.

Important sections:

- `[database]`: SQLite database path
- `[results_roots]`: Linux and Windows-visible VirPipa result roots
- `[exports]`: server-side LIMS export root
- `[cache]`: local cache for small imported artifacts
- `[auth]`: optional local login and session settings
- `[ui]`: table columns, defaults, labels, width caps, and highlight rules

Start from `virtitta.example.toml` for new deployments.

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

If restored runs need external Clarity metadata:

```bash
python -m virtitta.cli import-run \
  --config virtitta.toml \
  --run-dir /path/to/results/<run_name> \
  --clarity-sample-info /path/to/clarity_sample_info.json
```

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
- `reviewer`: QC, categories, groups, comments, read exports, and server-side LIMS export
- `commenter`: view, read exports, and add comments
- `viewer`: view and read exports only

## Deployment Notes

For short-term colleague feedback:

- create a micromamba environment for the web host
- run Virtitta through a systemd service
- put a reverse proxy in front for HTTPS
- enable auth and secure cookies

For longer-term production deployment:

- build a slim Python container
- mount `virtitta.toml`, the SQLite database directory, cache/export directories, and result roots
- keep TLS termination in the reverse proxy
- consider LDAP/AD as an auth provider while keeping Virtitta permissions internal
