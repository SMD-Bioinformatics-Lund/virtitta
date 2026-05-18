# Virtitta Plans And TODO

This file records future work discussed during development. Add new planned work here when it is not implemented in
the same change. Keep entries short enough to revisit and prune.

## Documentation Convention

- When a future feature, deployment idea, or deferred cleanup is discussed, add it here.
- Move implemented items to a completed section or remove them after the implementation is documented elsewhere.
- Prefer concrete next steps over broad intent.

## Open Plans

### Deployment And Packaging

Goal: move Virtitta from local development toward a web-server deployment for colleague feedback, then later to a
containerized deployment.

Next steps:

- inspect the current old web-server Virtitta instance
- document how the current instance is launched, proxied, configured, and updated
- prepare a short-term micromamba deployment recipe:
  - `environment.yml`
  - systemd service
  - reverse proxy with HTTPS
  - `auth.enabled = true`
  - `auth.cookie_secure = true`
- add a reproducible dependency lock strategy before wider deployment
- later build a slim Python container image
- mount config, database, cache/export directories, and result roots into the container

### LDAP Or AD Authentication

Goal: support institutional authentication without changing Virtitta's route permissions.

Next steps:

- keep Virtitta permissions role/capability-based internally
- add an auth-provider interface beside the current local provider
- map LDAP/AD users or groups to Virtitta roles
- document operational requirements once the local LDAP/AD details are known

### Isolate Clustering

Goal: add clustering from selected samples in the main table.

Expected first implementation:

- select samples in the main table
- derive or export FASTA inputs
- run trimming if needed
- align with MAFFT
- infer phylogeny with IQ-TREE 2
- store outputs in a predictable configured location
- expose tree viewing/downloading in the UI

Keep this config-driven:

- tool paths or container commands
- working/output directories
- enabled/disabled feature flag
- virus-specific defaults if HCV-specific assumptions are introduced

### Documentation Audit

Goal: keep the growing feature set discoverable.

Next steps:

- keep `docs/INTERFACE.md` updated when UI behavior changes
- keep `docs/ADMIN.md` updated when CLI commands or deployment assumptions change
- periodically compare `python -m virtitta.cli --help` with `docs/ADMIN.md`
- document any hidden or role-specific UI behavior when it is added

### Auth Test Cleanup

Goal: simplify auth-enabled test cases now that extra test packages are available in the development environment.

Context:

- Direct endpoint/ASGI harness tests are currently used where the local `TestClient` path hung with middleware
  early returns in this environment.

Next steps:

- re-check `TestClient` behavior in the updated conda environment
- if stable, convert auth smoke tests to client-style requests for readability
- keep at least one low-level ASGI test if it catches middleware redirects clearly
