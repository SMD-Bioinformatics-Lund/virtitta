from __future__ import annotations

import argparse
import getpass
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

    verify_cache = subparsers.add_parser("verify-cache", help="Verify cached output artifacts against remote files")
    add_config_argument(verify_cache)
    verify_target = verify_cache.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("--sample-run-id", default="", help="Verify one sample")
    verify_target.add_argument("--run-name", default="", help="Verify all samples in one run")
    verify_target.add_argument("--all-runs", action="store_true", help="Verify all imported runs except manual_failed_samples")
    verify_cache.add_argument(
        "--refresh",
        action="store_true",
        help="Rebuild configured cache entries from remote files before verification",
    )

    serve = subparsers.add_parser("serve", help="Run the FastAPI development server")
    add_config_argument(serve)
    serve.add_argument("--host", default=None, help="Override host from config")
    serve.add_argument("--port", type=int, default=None, help="Override port from config")

    create_user = subparsers.add_parser("create-user", help="Create a local Virtitta user")
    add_config_argument(create_user)
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--role", required=True, choices=["admin", "reviewer", "commenter", "viewer"])
    create_user.add_argument("--display-name", default="")
    create_user.add_argument("--password", default="", help="Password; prompt is used when omitted")

    list_users = subparsers.add_parser("list-users", help="List local Virtitta users")
    add_config_argument(list_users)

    set_user_role = subparsers.add_parser("set-user-role", help="Change a local user's role")
    add_config_argument(set_user_role)
    set_user_role.add_argument("--username", required=True)
    set_user_role.add_argument("--role", required=True, choices=["admin", "reviewer", "commenter", "viewer"])

    reset_user_password = subparsers.add_parser("reset-user-password", help="Reset a local user's password")
    add_config_argument(reset_user_password)
    reset_user_password.add_argument("--username", required=True)
    reset_user_password.add_argument("--password", default="", help="Password; prompt is used when omitted")

    enable_user = subparsers.add_parser("enable-user", help="Enable a local user")
    add_config_argument(enable_user)
    enable_user.add_argument("--username", required=True)

    disable_user = subparsers.add_parser("disable-user", help="Disable a local user and clear active sessions")
    add_config_argument(disable_user)
    disable_user.add_argument("--username", required=True)

    return parser


def print_import_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"Warning: {warning}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    from virtitta.app import create_app
    from virtitta.artifact_cache import CACHE_OK, cache_sample_outputs, verify_sample_cache
    from virtitta.auth import hash_password, normalize_username, validate_role
    from virtitta.config import load_config
    from virtitta.importer import import_all_roots_with_report, import_run_with_report, import_sample
    from virtitta.repository import (
        backfill_variant_af_counts,
        connect,
        create_auth_user,
        get_sample,
        get_samples_by_run,
        init_db,
        list_auth_users,
        list_samples_for_cache_verification,
        set_auth_user_enabled,
        set_auth_user_password,
        set_auth_user_role,
    )

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
        report = import_run_with_report(
            config,
            Path(args.run_dir),
            Path(args.clarity_sample_info) if args.clarity_sample_info else None,
        )
        print(f"Imported {report.imported} samples from {args.run_dir}")
        print_import_warnings(report.warnings)
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
        report = import_all_roots_with_report(config)
        print(f"Imported {report.imported} samples from configured roots")
        print_import_warnings(report.warnings)
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

    if args.command == "verify-cache":
        connection = connect(config.database.path)
        try:
            init_db(connection)
            if args.sample_run_id:
                sample = get_sample(connection, args.sample_run_id)
                sample_rows = [sample] if sample is not None else []
            elif args.run_name:
                sample_rows = get_samples_by_run(connection, args.run_name)
            else:
                sample_rows = list_samples_for_cache_verification(connection, all_runs=True)

            if args.refresh:
                for sample_row in sample_rows:
                    cache_sample_outputs(config, connection, sample_row)
                connection.commit()

            results = []
            for sample_row in sample_rows:
                results.extend(verify_sample_cache(config, connection, sample_row))
        finally:
            connection.close()

        if not sample_rows:
            print("No matching samples found")
            raise SystemExit(1)

        for item in results:
            print(
                "\t".join(
                    [
                        item["status"],
                        item["run_name"],
                        item["sample_run_id"],
                        item["output_key"],
                    ]
                )
            )
        failed = [item for item in results if item["status"] != CACHE_OK]
        print(f"Verified {len(results)} cached artifact(s); {len(failed)} issue(s).")
        if failed:
            raise SystemExit(1)
        return

    if args.command == "serve":
        import uvicorn

        host = args.host or config.app.host
        port = args.port or config.app.port
        uvicorn.run(create_app(args.config), host=host, port=port)
        return

    if args.command == "create-user":
        username = normalize_username(args.username)
        if not username:
            parser.error("--username cannot be blank")
        role = validate_role(args.role)
        password = args.password or getpass.getpass("Password: ")
        connection = connect(config.database.path)
        try:
            init_db(connection)
            create_auth_user(
                connection,
                username,
                hash_password(password, iterations=config.auth.pbkdf2_iterations),
                role,
                args.display_name.strip() or None,
            )
        finally:
            connection.close()
        print(f"Created user {username} with role {role}")
        return

    if args.command == "list-users":
        connection = connect(config.database.path)
        try:
            init_db(connection)
            users = list_auth_users(connection)
        finally:
            connection.close()
        for user in users:
            enabled = "enabled" if user["is_enabled"] else "disabled"
            display_name = user.get("display_name") or ""
            print("\t".join([user["username"], user["role"], enabled, display_name]))
        return

    if args.command == "set-user-role":
        username = normalize_username(args.username)
        if not username:
            parser.error("--username cannot be blank")
        role = validate_role(args.role)
        connection = connect(config.database.path)
        try:
            init_db(connection)
            updated = set_auth_user_role(connection, username, role)
        finally:
            connection.close()
        if not updated:
            print(f"User not found: {username}")
            raise SystemExit(1)
        print(f"Set {username} role to {role}")
        return

    if args.command == "reset-user-password":
        username = normalize_username(args.username)
        if not username:
            parser.error("--username cannot be blank")
        password = args.password or getpass.getpass("Password: ")
        connection = connect(config.database.path)
        try:
            init_db(connection)
            updated = set_auth_user_password(
                connection,
                username,
                hash_password(password, iterations=config.auth.pbkdf2_iterations),
            )
        finally:
            connection.close()
        if not updated:
            print(f"User not found: {username}")
            raise SystemExit(1)
        print(f"Reset password for {username}")
        return

    if args.command in {"enable-user", "disable-user"}:
        username = normalize_username(args.username)
        if not username:
            parser.error("--username cannot be blank")
        enabled = args.command == "enable-user"
        connection = connect(config.database.path)
        try:
            init_db(connection)
            updated = set_auth_user_enabled(connection, username, enabled)
        finally:
            connection.close()
        if not updated:
            print(f"User not found: {username}")
            raise SystemExit(1)
        print(f"{'Enabled' if enabled else 'Disabled'} user {username}")
        return

    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
