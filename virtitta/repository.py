from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path


SORTABLE_COLUMNS = {
    "generated_date": "s.generated_date",
    "sample_run_id": "s.sample_run_id",
    "sample_id": "s.sample_id",
    "lid": "s.lid",
    "run_name": "s.run_name",
    "typing_report_subtype": "s.typing_report_subtype",
    "typing_main_blast_identity": "s.typing_main_blast_identity",
    "resistance_summary": "s.sample_id",
    "host_filter_reads_in": "s.host_filter_reads_in",
    "host_filter_reads_removed_proportion": "s.host_filter_reads_removed_proportion",
    "qc_coverage_pct": "s.qc_coverage_pct",
    "qc_mean_depth": "s.qc_mean_depth",
    "qc_coverage_1x_pct": "s.qc_coverage_1x_pct",
    "qc_coverage_10x_pct": "s.qc_coverage_10x_pct",
    "qc_coverage_100x_pct": "s.qc_coverage_100x_pct",
    "qc_coverage_1000x_pct": "s.qc_coverage_1000x_pct",
    "sample_metadata_ct": "s.sample_metadata_ct",
    "sample_metadata_library_concentration_ng_ul": "s.sample_metadata_library_concentration_ng_ul",
    "sample_metadata_library_fragment_length_bp": "s.sample_metadata_library_fragment_length_bp",
    "qc_status": "COALESCE(r.qc_status, 'unreviewed')",
    "comment_count": "comment_count",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: sqlite3.Row | Mapping | None) -> dict | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    return dict(row)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    existing = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in existing:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_name TEXT PRIMARY KEY,
            sample_count INTEGER NOT NULL,
            pipeline_name TEXT,
            virus TEXT,
            source_root_name TEXT,
            run_relpath TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS samples (
            sample_run_id TEXT PRIMARY KEY,
            run_name TEXT NOT NULL REFERENCES runs(run_name) ON DELETE CASCADE,
            generated_date TEXT,
            sample_id TEXT NOT NULL,
            lid TEXT,
            source_root_name TEXT,
            sample_results_relpath TEXT NOT NULL,
            typing_report_subtype TEXT,
            typing_main_blast_identity REAL,
            host_filter_reads_in INTEGER,
            host_filter_reads_removed_proportion REAL,
            qc_coverage_pct REAL,
            qc_mean_depth REAL,
            qc_coverage_1x_pct REAL,
            qc_coverage_10x_pct REAL,
            qc_coverage_100x_pct REAL,
            qc_coverage_1000x_pct REAL,
            sample_metadata_ct REAL,
            sample_metadata_library_concentration_ng_ul REAL,
            sample_metadata_library_fragment_length_bp INTEGER,
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sample_review (
            sample_run_id TEXT PRIMARY KEY REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            qc_status TEXT NOT NULL DEFAULT 'unreviewed',
            updated_at TEXT NOT NULL,
            updated_by TEXT
        );

        CREATE TABLE IF NOT EXISTS sample_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_run_id TEXT NOT NULL REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            author TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_samples_run_name ON samples(run_name);
        CREATE INDEX IF NOT EXISTS idx_samples_sample_id ON samples(sample_id);
        CREATE INDEX IF NOT EXISTS idx_samples_lid ON samples(lid);
        CREATE INDEX IF NOT EXISTS idx_comments_sample_run_id ON sample_comments(sample_run_id);
        """
    )
    _ensure_column(connection, "samples", "generated_date", "TEXT")
    connection.commit()


def upsert_run(connection: sqlite3.Connection, run_record: dict) -> None:
    connection.execute(
        """
        INSERT INTO runs (
            run_name, sample_count, pipeline_name, virus, source_root_name, run_relpath, imported_at
        )
        VALUES (:run_name, :sample_count, :pipeline_name, :virus, :source_root_name, :run_relpath, :imported_at)
        ON CONFLICT(run_name) DO UPDATE SET
            sample_count = excluded.sample_count,
            pipeline_name = excluded.pipeline_name,
            virus = excluded.virus,
            source_root_name = excluded.source_root_name,
            run_relpath = excluded.run_relpath,
            imported_at = excluded.imported_at
        """,
        run_record,
    )


def upsert_sample(connection: sqlite3.Connection, sample_record: dict) -> None:
    connection.execute(
        """
        INSERT INTO samples (
            sample_run_id, run_name, generated_date, sample_id, lid, source_root_name, sample_results_relpath,
            typing_report_subtype, typing_main_blast_identity,
            host_filter_reads_in, host_filter_reads_removed_proportion,
            qc_coverage_pct, qc_mean_depth, qc_coverage_1x_pct, qc_coverage_10x_pct,
            qc_coverage_100x_pct, qc_coverage_1000x_pct,
            sample_metadata_ct, sample_metadata_library_concentration_ng_ul, sample_metadata_library_fragment_length_bp,
            raw_json, imported_at
        )
        VALUES (
            :sample_run_id, :run_name, :generated_date, :sample_id, :lid, :source_root_name, :sample_results_relpath,
            :typing_report_subtype, :typing_main_blast_identity,
            :host_filter_reads_in, :host_filter_reads_removed_proportion,
            :qc_coverage_pct, :qc_mean_depth, :qc_coverage_1x_pct, :qc_coverage_10x_pct,
            :qc_coverage_100x_pct, :qc_coverage_1000x_pct,
            :sample_metadata_ct, :sample_metadata_library_concentration_ng_ul, :sample_metadata_library_fragment_length_bp,
            :raw_json, :imported_at
        )
        ON CONFLICT(sample_run_id) DO UPDATE SET
            run_name = excluded.run_name,
            generated_date = excluded.generated_date,
            sample_id = excluded.sample_id,
            lid = excluded.lid,
            source_root_name = excluded.source_root_name,
            sample_results_relpath = excluded.sample_results_relpath,
            typing_report_subtype = excluded.typing_report_subtype,
            typing_main_blast_identity = excluded.typing_main_blast_identity,
            host_filter_reads_in = excluded.host_filter_reads_in,
            host_filter_reads_removed_proportion = excluded.host_filter_reads_removed_proportion,
            qc_coverage_pct = excluded.qc_coverage_pct,
            qc_mean_depth = excluded.qc_mean_depth,
            qc_coverage_1x_pct = excluded.qc_coverage_1x_pct,
            qc_coverage_10x_pct = excluded.qc_coverage_10x_pct,
            qc_coverage_100x_pct = excluded.qc_coverage_100x_pct,
            qc_coverage_1000x_pct = excluded.qc_coverage_1000x_pct,
            sample_metadata_ct = excluded.sample_metadata_ct,
            sample_metadata_library_concentration_ng_ul = excluded.sample_metadata_library_concentration_ng_ul,
            sample_metadata_library_fragment_length_bp = excluded.sample_metadata_library_fragment_length_bp,
            raw_json = excluded.raw_json,
            imported_at = excluded.imported_at
        """,
        sample_record,
    )
    connection.execute(
        """
        INSERT INTO sample_review (sample_run_id, qc_status, updated_at)
        VALUES (?, 'unreviewed', ?)
        ON CONFLICT(sample_run_id) DO NOTHING
        """,
        (sample_record["sample_run_id"], sample_record["imported_at"]),
    )


def list_runs(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute("SELECT run_name FROM runs ORDER BY run_name DESC").fetchall()
    return [row["run_name"] for row in rows]


def list_subtypes(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT typing_report_subtype
        FROM samples
        WHERE typing_report_subtype IS NOT NULL AND typing_report_subtype != ''
        ORDER BY typing_report_subtype ASC
        """
    ).fetchall()
    return [row["typing_report_subtype"] for row in rows]


def list_samples(
    connection: sqlite3.Connection,
    *,
    search: str = "",
    run_name: str = "",
    subtype: str = "",
    qc_status: str = "",
    min_coverage_pct: float | None = None,
    min_mean_depth: float | None = None,
    min_blast_identity: float | None = None,
    max_ct: float | None = None,
    sort: str = "run_name",
    desc: bool = True,
) -> list[dict]:
    clauses = []
    params: list[object] = []

    if search:
        clauses.append("(s.sample_id LIKE ? OR COALESCE(s.lid, '') LIKE ? OR s.run_name LIKE ?)")
        needle = f"%{search}%"
        params.extend([needle, needle, needle])
    if run_name:
        clauses.append("s.run_name = ?")
        params.append(run_name)
    if subtype:
        clauses.append("s.typing_report_subtype = ?")
        params.append(subtype)
    if qc_status:
        clauses.append("COALESCE(r.qc_status, 'unreviewed') = ?")
        params.append(qc_status)
    if min_coverage_pct is not None:
        clauses.append("s.qc_coverage_pct >= ?")
        params.append(min_coverage_pct)
    if min_mean_depth is not None:
        clauses.append("s.qc_mean_depth >= ?")
        params.append(min_mean_depth)
    if min_blast_identity is not None:
        clauses.append("s.typing_main_blast_identity >= ?")
        params.append(min_blast_identity)
    if max_ct is not None:
        clauses.append("s.sample_metadata_ct <= ?")
        params.append(max_ct)

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    sort_expr = SORTABLE_COLUMNS.get(sort, SORTABLE_COLUMNS["run_name"])
    order = "DESC" if desc else "ASC"

    query = f"""
        SELECT
            s.*,
            COALESCE(r.qc_status, 'unreviewed') AS qc_status,
            r.updated_at AS qc_updated_at,
            COUNT(c.id) AS comment_count,
            (
                SELECT GROUP_CONCAT(preview, '\n---\n')
                FROM (
                    SELECT
                        CASE
                            WHEN sc.author IS NOT NULL AND sc.author != ''
                                THEN sc.author || ': '
                            ELSE ''
                        END ||
                        SUBSTR(REPLACE(REPLACE(sc.body, CHAR(13), ' '), CHAR(10), ' '), 1, 160) AS preview
                    FROM sample_comments sc
                    WHERE sc.sample_run_id = s.sample_run_id
                    ORDER BY sc.created_at DESC, sc.id DESC
                    LIMIT 3
                )
            ) AS comment_preview
        FROM samples s
        LEFT JOIN sample_review r ON r.sample_run_id = s.sample_run_id
        LEFT JOIN sample_comments c ON c.sample_run_id = s.sample_run_id
        {where_sql}
        GROUP BY s.sample_run_id
        ORDER BY {sort_expr} {order}, s.sample_run_id ASC
    """
    return [_row_to_dict(row) for row in connection.execute(query, params).fetchall()]


def get_sample(connection: sqlite3.Connection, sample_run_id: str) -> dict | None:
    row = connection.execute(
        """
        SELECT
            s.*,
            COALESCE(r.qc_status, 'unreviewed') AS qc_status,
            r.updated_at AS qc_updated_at,
            r.updated_by AS qc_updated_by
        FROM samples s
        LEFT JOIN sample_review r ON r.sample_run_id = s.sample_run_id
        WHERE s.sample_run_id = ?
        """,
        (sample_run_id,),
    ).fetchone()
    return _row_to_dict(row)


def get_comments(connection: sqlite3.Connection, sample_run_id: str) -> list[dict]:
    return [
        _row_to_dict(row)
        for row in connection.execute(
        """
        SELECT id, sample_run_id, body, author, created_at
        FROM sample_comments
        WHERE sample_run_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (sample_run_id,),
    ).fetchall()
    ]


def update_qc_status(connection: sqlite3.Connection, sample_run_ids: list[str], status: str, updated_by: str | None = None) -> None:
    now = utc_now()
    for sample_run_id in sample_run_ids:
        connection.execute(
            """
            INSERT INTO sample_review (sample_run_id, qc_status, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sample_run_id) DO UPDATE SET
                qc_status = excluded.qc_status,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (sample_run_id, status, now, updated_by),
        )
    connection.commit()


def add_comment(connection: sqlite3.Connection, sample_run_id: str, body: str, author: str | None = None) -> None:
    connection.execute(
        """
        INSERT INTO sample_comments (sample_run_id, body, author, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (sample_run_id, body.strip(), author or None, utc_now()),
    )
    connection.commit()


def delete_comment(connection: sqlite3.Connection, sample_run_id: str, comment_id: int) -> None:
    connection.execute(
        """
        DELETE FROM sample_comments
        WHERE id = ? AND sample_run_id = ?
        """,
        (comment_id, sample_run_id),
    )
    connection.commit()


def delete_samples(connection: sqlite3.Connection, sample_run_ids: list[str]) -> None:
    if not sample_run_ids:
        return

    run_names = [
        row["run_name"]
        for row in connection.execute(
            f"""
            SELECT DISTINCT run_name
            FROM samples
            WHERE sample_run_id IN ({",".join("?" for _ in sample_run_ids)})
            """,
            sample_run_ids,
        ).fetchall()
    ]

    connection.execute(
        f"""
        DELETE FROM samples
        WHERE sample_run_id IN ({",".join("?" for _ in sample_run_ids)})
        """,
        sample_run_ids,
    )

    for run_name in run_names:
        remaining = connection.execute(
            "SELECT COUNT(*) AS count FROM samples WHERE run_name = ?",
            (run_name,),
        ).fetchone()["count"]
        if remaining:
            connection.execute(
                "UPDATE runs SET sample_count = ? WHERE run_name = ?",
                (remaining, run_name),
            )
        else:
            connection.execute("DELETE FROM runs WHERE run_name = ?", (run_name,))

    connection.commit()


def raw_json_for_sample(row: Mapping) -> dict:
    return json.loads(row["raw_json"])
