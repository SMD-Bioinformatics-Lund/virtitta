from __future__ import annotations

import argparse
from pathlib import Path


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="Path to virtitta TOML config")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Virtitta admin CLI")
    add_config_argument(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize the SQLite database")
    add_config_argument(init_db)

    import_run = subparsers.add_parser("import-run", help="Import one completed virpipa run")
    add_config_argument(import_run)
    import_run.add_argument("--run-dir", required=True, help="Path to results/<run_name>")

    import_root = subparsers.add_parser("import-root", help="Import every run under the configured results roots")
    add_config_argument(import_root)

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
    from virtitta.importer import import_all_roots, import_run
    from virtitta.repository import connect, init_db

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
        imported = import_run(config, Path(args.run_dir))
        print(f"Imported {imported} samples from {args.run_dir}")
        return

    if args.command == "import-root":
        imported = import_all_roots(config)
        print(f"Imported {imported} samples from configured roots")
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
