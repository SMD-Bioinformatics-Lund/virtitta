# Virtitta Plans And TODO

This file records future work discussed during development. Add new planned work here when it is not implemented in
the same change. Keep entries short enough to revisit and prune.

## Documentation Convention

- When a future feature, deployment idea, or deferred cleanup is discussed, add it here.
- Move implemented items to a completed section or remove them after the implementation is documented elsewhere.
- Prefer concrete next steps over broad intent.

## Open Plans

### LDAP Or AD Authentication

Goal: support institutional authentication without changing Virtitta's route permissions.

Next steps:

- keep Virtitta permissions role/capability-based internally
- add an auth-provider interface beside the current local provider
- map LDAP/AD users or groups to Virtitta roles
- document operational requirements once the local LDAP/AD details are known

### Documentation Audit

Goal: keep the growing feature set discoverable.

Next steps:

- keep `docs/INTERFACE.md` updated when UI behavior changes
- keep `docs/ADMIN.md` updated when CLI commands or deployment assumptions change
- periodically compare `python -m virtitta.cli --help` with `docs/ADMIN.md`
- document any hidden or role-specific UI behavior when it is added

### Administrator-Defined Main Table Views

Goal: optionally provide centrally configured main-table column sets in addition to implemented per-user presets.

Next steps:

- keep column identifiers stable and config-driven
- define a small `[ui.table_views]` TOML shape with a name, label, columns, and optional default sort
- merge configured views into the existing preset selector without allowing users to overwrite or delete them

### Auth Test Cleanup

Goal: simplify auth-enabled test cases now that extra test packages are available in the development environment.

Context:

- Direct endpoint/ASGI harness tests are currently used where the local `TestClient` path hung with middleware
  early returns in this environment.

Next steps:

- re-check `TestClient` behavior in the updated conda environment
- if stable, convert auth smoke tests to client-style requests for readability
- keep at least one low-level ASGI test if it catches middleware redirects clearly

## Implemented Plans

### Docker And Apache Deployment

Added a reusable micromamba-based container with standalone Docker Compose operation, one-shot CLI commands, external
configuration and data mounts, configurable application root paths, an Apache `/virtitta` proxy example, and a thin
wrapper for the existing `.sqlimport` runner.

### Isolate Clustering V1

Implemented selected-sample clustering behind `[cluster].enabled`. The first version prepares imported IUPAC FASTA
records, prepares FASTA internally, aligns with MAFFT, builds a tree with IQ-TREE 3, stores artifacts under a configured
output root, and exposes downloads plus GrapeTree handoff links.

### webIGV Fallback

Implemented a browser-based IGV fallback for users without desktop drive mappings. Desktop IGV remains the preferred
action, while webIGV serves imported, indexed track files through authenticated Virtitta routes using the server's
read-only Linux result-root mounts.
