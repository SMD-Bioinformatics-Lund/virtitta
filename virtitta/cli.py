from __future__ import annotations

import argparse
from pathlib import Path


def add_config_argument(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--config", default=None, required=required, help="Path to virtitta TOML config")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Virtitta admin CLI")
    add_config_argument(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize the SQLite database")
    add_config_argument(init_db)

    import_run = subparsers.add_parser("import-run", help="Import one completed virpipa run")
    add_config_argument(import_run)
    import_run.add_argument("--run-dir", required=True, help="Path to results/<run_name>")
    import_run.add_argument(
        "--clarity-sample-info",
        default="",
        help="Optional path to clarity_sample_info.json for restored or relocated runs",
    )

    import_sample = subparsers.add_parser("import-sample", help="Import one failed sample without QC output")
    add_config_argument(import_sample, required=True)
    import_sample.add_argument("--run-dir", default="", help="Optional path to results/<run_name>")
    import_sample.add_argument("--sample-id", required=True, help="Sample ID used by virpipa")
    import_sample.add_argument("--lid", required=True, help="LID shown as the primary UI identifier")
    import_sample.add_argument(
        "--ct",
        type=float,
        default=None,
        help="Optional CT value when Clarity JSON is unavailable",
    )
    import_sample.add_argument(
        "--library-concentration",
        type=float,
        default=None,
        help="Optional library concentration in ng/ul when Clarity JSON is unavailable",
    )
    import_sample.add_argument(
        "--clarity-sample-info",
        default="",
        help="Optional path to clarity_sample_info.json for CT and library metadata",
    )

    import_root = subparsers.add_parser("import-root", help="Import every run under the configured results roots")
    add_config_argument(import_root)

    backfill_af_counts = subparsers.add_parser(
        "backfill-af-counts",
        help="Backfill flattened AF count columns from samples.raw_json",
    )
    add_config_argument(backfill_af_counts)

    serve = subparsers.add_parser("serve", help="Run the FastAPI development server")
    add_config_argument(serve)
    serve.add_argument("--host", default=None, help="Override host from config")
    serve.add_argument("--port", type=int, default=None, help="Override port from config")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    from virtitta.app import create_app
    from virtitta.config import load_config
    from virtitta.importer import import_all_roots, import_run, import_sample
    from virtitta.repository import backfill_variant_af_counts, connect, init_db

    config = load_config(args.config or "virtitta.toml")

    if args.command == "init-db":
        connection = connect(config.database.path)
        try:
            init_db(connection)
        finally:
            connection.close()
        print(f"Initialized database at {config.database.path}")
        return

    if args.command == "import-run":
        imported = import_run(
            config,
            Path(args.run_dir),
            Path(args.clarity_sample_info) if args.clarity_sample_info else None,
        )
        print(f"Imported {imported} samples from {args.run_dir}")
        return

    if args.command == "import-sample":
        sample_run_id = import_sample(
            config,
            args.sample_id,
            args.lid,
            run_dir=Path(args.run_dir) if args.run_dir else None,
            clarity_sample_info_path=Path(args.clarity_sample_info) if args.clarity_sample_info else None,
            ct=args.ct,
            library_concentration_ng_ul=args.library_concentration,
        )
        print(f"Imported failed sample {sample_run_id}")
        return

    if args.command == "import-root":
        imported = import_all_roots(config)
        print(f"Imported {imported} samples from configured roots")
        return

    if args.command == "backfill-af-counts":
        connection = connect(config.database.path)
        try:
            init_db(connection)
            updated = backfill_variant_af_counts(connection)
        finally:
            connection.close()
        print(f"Backfilled AF counts for {updated} sample(s)")
        return

    if args.command == "serve":
        import uvicorn

        host = args.host or config.app.host
        port = args.port or config.app.port
        uvicorn.run(create_app(args.config), host=host, port=port)
        return

    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
