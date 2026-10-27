# Isolated VirPipa and Virtitta development testing

Use micromamba/conda for the dev web instance; leave production Docker, its
configuration, database, import runner and LIMS destinations untouched. This
procedure does not require changes to `bnf-infrastructure` or its generating service.

## 1. Select code without disturbing production

Use a dedicated checkout/worktree for each development branch. Record both Git
commits with `git -C /path/to/checkout log -1 --oneline` and check `git status --short`.
Push VirPipa to the Hopper bare repository, then explicitly update the development
working checkout before submission. The full-analysis launcher does not pull code.
Do not update that checkout while jobs are queued or running. Do not deploy to
`/fs1/pipelines/virpipa/current` or use Virtitta production image-deployment scripts.

General dev setup changes are merged independently from resistance or other
feature changes. The `dev-instance-setup` branch does not itself include the resistance
redesign. Merge/cherry-pick its reviewed commits into the feature branch before
testing that feature; do not replace the feature branch with this setup branch.

## 2. Launch VirPipa through the existing Hopper launcher

Use the original service-generated `multi_HCV.csv` unchanged. The launcher reads
its `assay` field to choose configuration, then passes the same file to VirPipa as
`--csv`. VirPipa uses the sample/read/run fields. No extra samplesheet or `--assay`
option is required or supported. The CSV must already contain the intended samples;
do not launch an entire collection unintentionally.

For an isolated manual run, override the pipeline and all writable destinations;
the original assay label remains in launcher job names. These example paths are
on Hopper, not the laptop or Lennart:

```bash
export VIRPIPA_DEV_RUN=/fs1/jonas/virpipa/dev_validation/resistance_review_01
export VIRPIPA_DEV_RESULTS=/fs1/results_dev/virpipa/hcv
export VIRPIPA_SERVICE_CSV=/path/to/run/pipeline/multi_HCV.csv
mkdir -p "$VIRPIPA_DEV_RUN"

/fs2/sw/bnf-scripts/start_nextflow_analysis.pl "$VIRPIPA_SERVICE_CSV" \
  --id resistance_review_01 \
  --pipeline "/fs1/jonas/src/virpipa -profile slurm,hpc,apptainer -ansi-log false" \
  --executor slurm --queue normal \
  --nextflow_version 26.04.3 --java_version 23.0.2 --singularity_version 3.8.0 \
  --outdir "$VIRPIPA_DEV_RESULTS" \
  --workdir "$VIRPIPA_DEV_RUN" \
  --custom_flags "--publish_mode debug --container_runtime singularity --completion_log_dir $VIRPIPA_DEV_RUN/completion_logs --virtitta_import_dir $VIRPIPA_DEV_RUN/import_markers --virtitta_run_dir $VIRPIPA_DEV_RESULTS" \
  --nostart
```

Inspect the generated sbatch file before submitting it using `sbatch /path/printed/by/launcher`.
Verify the code revision, runtime modules and every destination. In particular:

- Results go to `results_dev`; work/reports/logs go to the private campaign directory.
- Completion logs must not go to `/fs1/results/cron/logs`.
- Import markers must not go to `/fs1/results/cron/virtitta`.
- Marker contents must not point at `/access/virpipa/hcv`.

The installed `HCV-dev` definition may still contain production defaults. An
`--outdir` override alone is not enough. Do not pass an empty string to disable
marker creation: Nextflow can interpret it as a boolean. An isolated, unwatched
marker directory is deliberate; do not connect the production runner to it.

Choose a new campaign work directory for another revision. Avoid overwriting
archives currently being reviewed. For side-by-side results with identical sample/run
identities, use separate result roots and separate Virtitta databases.

## 3. Configure the dev Virtitta instance on Lennart

Copy `virtitta.dev.example.toml` to `virtitta.dev.toml`; the latter and `data_dev/`
are ignored by Git. Do not reuse or overwrite `virtitta_lennart.toml`.
All relative writable paths are resolved against the config directory.

The template isolates the database, output cache, clustering files and local LIMS
exports. It enables authentication, uses a distinct cookie name and deliberately
omits the production LIMS ingest directory. Clustering is initially disabled.

Before importing, verify that `results_roots[].linux_path` exists on Lennart and
points to the dev files. Hopper paths are not proof of Lennart mount visibility.
Likewise verify `windows_path` on the reviewer laptop; the template Q: mapping is
an example. Do not leave any production results root in the dev config.

Example source/config/environment locations below must be adjusted to your checkout:

```bash
export VIRTITTA_SRC=/data/bnf/dev/jonas/hcv/virtitta-dev-setup
export VIRTITTA_DEV_CONFIG="$VIRTITTA_SRC/virtitta.dev.toml"
export MAMBA_ROOT_PREFIX=/data/bnf/dev/jonas/hcv/micromamba
export VIRTITTA_MAMBA="$MAMBA_ROOT_PREFIX/micromamba"
export VIRTITTA_DEV_ENV="$MAMBA_ROOT_PREFIX/envs/virtitta"
export PYTHONPATH="$VIRTITTA_SRC"

# Copy once, then edit and verify the local config; do not overwrite an existing one.
test ! -e "$VIRTITTA_DEV_CONFIG" && \
  cp "$VIRTITTA_SRC/virtitta.dev.example.toml" "$VIRTITTA_DEV_CONFIG"

"$VIRTITTA_MAMBA" run -p "$VIRTITTA_DEV_ENV" python -c \
  'import virtitta; print(virtitta.__file__)'
"$VIRTITTA_MAMBA" run -p "$VIRTITTA_DEV_ENV" python -m virtitta.cli init-db \
  --config "$VIRTITTA_DEV_CONFIG"
"$VIRTITTA_MAMBA" run -p "$VIRTITTA_DEV_ENV" python -m virtitta.cli create-user \
  --config "$VIRTITTA_DEV_CONFIG" --username devadmin --role admin
"$VIRTITTA_MAMBA" run -p "$VIRTITTA_DEV_ENV" python -m virtitta.cli serve \
  --config "$VIRTITTA_DEV_CONFIG"
```

Confirm that the printed module path belongs to the intended checkout. Passwords
are prompted for; use dedicated dev credentials, not a production password.
The displayed version comes from that checkout's Git description at startup;
Docker's `VIRTITTA_VERSION` remains an explicit override. Unset a stale override
in the dev shell if necessary and restart after code changes.
With a fresh database the users are separate too. Start only one server per dev
database: startup resets queued/running cluster jobs in that database.

The template listens on server loopback. From the laptop:

```bash
ssh -N -L 15812:127.0.0.1:5812 jonas@SERVER_RUNNING_VIRTITTA
```

Open `http://127.0.0.1:15812/virtitta/`. For trusted direct network testing, append
`--host 0.0.0.0` to `serve` and verify firewall restrictions. Prefer HTTPS or an SSH
tunnel for sensitive data. `cookie_secure = false` is for controlled HTTP testing;
use `true` behind HTTPS. Cookies are not port-isolated, hence the distinct dev name.

To test clustering later, verify `mafft`, `iqtree3` and `samtools` inside the same
environment, enable clustering and set its dev URLs. `cluster.public_base_url` must
be the dev endpoint reachable by GrapeTree, never the production Virtitta URL or a
laptop-only loopback tunnel.

## 4. Manually import and validate

Keep using the same environment, checkout and config for all commands. After a
completed pipeline run is visible on Lennart:

```bash
"$VIRTITTA_MAMBA" run -p "$VIRTITTA_DEV_ENV" python -m virtitta.cli import-run \
  --config "$VIRTITTA_DEV_CONFIG" \
  --run-dir /fs1/results_dev/virpipa/hcv/SEQUENCING_RUN
```

Use the actual Lennart-visible path if different. Flat and legacy nested sample
archives are supported. Import one completed run manually; do not use production
Docker import wrappers, production cron or `import-root` during the initial check.

Confirm the dev title, expected code version, sample count, dev result paths, login
and desktop/webIGV file loading before expanding to 10–20 representative samples.
Use the feature branch-specific validation checklist for resistance details.
Re-import updates automated data while preserving comments, QC and curator state.

Nothing here migrates production data or requires changes to the service-generated
CSV, the shared launcher, its configuration, or production Docker. Deploy/promotion
is a separate reviewed operation.
