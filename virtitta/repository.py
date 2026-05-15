from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path


SORTABLE_COLUMNS = {
    "sequencing_date": "s.sequencing_date",
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
    "variant_af_count_005": "s.variant_af_count_005",
    "variant_af_count_01": "s.variant_af_count_01",
    "variant_af_count_015": "s.variant_af_count_015",
    "variant_af_count_02": "s.variant_af_count_02",
    "variant_af_count_03": "s.variant_af_count_03",
    "variant_af_count_04": "s.variant_af_count_04",
    "sample_metadata_ct": "s.sample_metadata_ct",
    "sample_metadata_library_concentration_ng_ul": "s.sample_metadata_library_concentration_ng_ul",
    "sample_metadata_library_fragment_length_bp": "s.sample_metadata_library_fragment_length_bp",
    "qc_status": "COALESCE(r.qc_status, 'unreviewed')",
    "sample_category": "COALESCE(a.sample_category, '')",
    "manual_groups": "manual_groups",
    "comment_count": "comment_count",
}

OVERRIDABLE_SAMPLE_FIELDS = {
    "lid",
    "sequencing_date",
    "sample_metadata_ct",
    "sample_metadata_library_concentration_ng_ul",
    "typing_report_subtype",
}

NUMERIC_OVERRIDE_FIELDS = {
    "sample_metadata_ct",
    "sample_metadata_library_concentration_ng_ul",
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


def _sequencing_date_from_run_name(run_name: object, fallback_date: str | None) -> str | None:
    text = str(run_name or "")
    prefix = text[:6]
    if len(prefix) == 6 and prefix.isdigit():
        try:
            return datetime.strptime(prefix, "%y%m%d").date().isoformat()
        except ValueError:
            pass
    return fallback_date


def _backfill_sequencing_dates(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT sample_run_id, run_name, generated_date
        FROM samples
        WHERE sequencing_date IS NULL OR sequencing_date = ''
        """
    ).fetchall()
    for row in rows:
        sequencing_date = _sequencing_date_from_run_name(row["run_name"], row["generated_date"])
        if sequencing_date is None:
            continue
        connection.execute(
            "UPDATE samples SET sequencing_date = ? WHERE sample_run_id = ?",
            (sequencing_date, row["sample_run_id"]),
        )


def _coerce_override_value(field_name: str, value: str) -> object:
    if field_name in NUMERIC_OVERRIDE_FIELDS:
        return float(value)
    return value


def _normalize_override_value(field_name: str, value: object) -> str | None:
    if field_name not in OVERRIDABLE_SAMPLE_FIELDS:
        raise ValueError(f"Unsupported override field: {field_name}")
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if field_name in NUMERIC_OVERRIDE_FIELDS:
        float(text)
    return text


def _typed_override_equal(field_name: str, left: object, right: object) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    if left in (None, "") or right in (None, ""):
        return False
    if field_name in NUMERIC_OVERRIDE_FIELDS:
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    return str(left) == str(right)


def _apply_sample_overrides(row: dict | None, overrides: dict[str, str] | None = None) -> dict | None:
    if row is None:
        return None
    overrides = overrides or {}
    overridden_fields = []
    for field_name in OVERRIDABLE_SAMPLE_FIELDS:
        row[f"imported_{field_name}"] = row.get(field_name)
        is_overridden = field_name in overrides
        row[f"{field_name}_overridden"] = is_overridden
        if is_overridden:
            row[field_name] = _coerce_override_value(field_name, overrides[field_name])
            overridden_fields.append(field_name)
    row["overridden_fields"] = overridden_fields
    return row


def get_sample_field_overrides(connection: sqlite3.Connection, sample_run_id: str) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT field_name, value_text
        FROM sample_field_overrides
        WHERE sample_run_id = ?
        """,
        (sample_run_id,),
    ).fetchall()
    return {row["field_name"]: row["value_text"] for row in rows}


def _sample_field_overrides_for_rows(connection: sqlite3.Connection, sample_run_ids: list[str]) -> dict[str, dict[str, str]]:
    if not sample_run_ids:
        return {}
    rows = connection.execute(
        f"""
        SELECT sample_run_id, field_name, value_text
        FROM sample_field_overrides
        WHERE sample_run_id IN ({",".join("?" for _ in sample_run_ids)})
        """,
        sample_run_ids,
    ).fetchall()
    overrides: dict[str, dict[str, str]] = {}
    for row in rows:
        overrides.setdefault(row["sample_run_id"], {})[row["field_name"]] = row["value_text"]
    return overrides


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
            sequencing_date TEXT,
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
            variant_af_count_005 INTEGER,
            variant_af_count_01 INTEGER,
            variant_af_count_015 INTEGER,
            variant_af_count_02 INTEGER,
            variant_af_count_03 INTEGER,
            variant_af_count_04 INTEGER,
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

        CREATE TABLE IF NOT EXISTS sample_annotations (
            sample_run_id TEXT PRIMARY KEY REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            sample_category TEXT
        );

        CREATE TABLE IF NOT EXISTS sample_field_overrides (
            sample_run_id TEXT NOT NULL REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            value_text TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT,
            PRIMARY KEY (sample_run_id, field_name)
        );

        CREATE TABLE IF NOT EXISTS sample_group_memberships (
            sample_run_id TEXT NOT NULL REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            group_name TEXT NOT NULL,
            PRIMARY KEY (sample_run_id, group_name)
        );

        CREATE TABLE IF NOT EXISTS sample_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_run_id TEXT NOT NULL REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            author TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS output_cache (
            sample_run_id TEXT NOT NULL REFERENCES samples(sample_run_id) ON DELETE CASCADE,
            output_key TEXT NOT NULL,
            remote_relpath TEXT NOT NULL,
            cached_relpath TEXT NOT NULL,
            remote_size INTEGER NOT NULL,
            remote_mtime_ns INTEGER NOT NULL,
            cached_sha256 TEXT NOT NULL,
            cached_at TEXT NOT NULL,
            verified_at TEXT,
            PRIMARY KEY (sample_run_id, output_key)
        );

        CREATE INDEX IF NOT EXISTS idx_samples_run_name ON samples(run_name);
        CREATE INDEX IF NOT EXISTS idx_samples_sample_id ON samples(sample_id);
        CREATE INDEX IF NOT EXISTS idx_samples_lid ON samples(lid);
        CREATE INDEX IF NOT EXISTS idx_annotations_sample_category ON sample_annotations(sample_category);
        CREATE INDEX IF NOT EXISTS idx_group_memberships_group_name ON sample_group_memberships(group_name);
        CREATE INDEX IF NOT EXISTS idx_comments_sample_run_id ON sample_comments(sample_run_id);
        CREATE INDEX IF NOT EXISTS idx_output_cache_output_key ON output_cache(output_key);
        """
    )
    _ensure_column(connection, "samples", "sequencing_date", "TEXT")
    _ensure_column(connection, "samples", "generated_date", "TEXT")
    _ensure_column(connection, "samples", "variant_af_count_005", "INTEGER")
    _ensure_column(connection, "samples", "variant_af_count_01", "INTEGER")
    _ensure_column(connection, "samples", "variant_af_count_015", "INTEGER")
    _ensure_column(connection, "samples", "variant_af_count_02", "INTEGER")
    _ensure_column(connection, "samples", "variant_af_count_03", "INTEGER")
    _ensure_column(connection, "samples", "variant_af_count_04", "INTEGER")
    _backfill_sequencing_dates(connection)
    connection.commit()


def backfill_variant_af_counts(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """
        SELECT
            sample_run_id,
            raw_json,
            variant_af_count_005,
            variant_af_count_01,
            variant_af_count_015,
            variant_af_count_02,
            variant_af_count_03,
            variant_af_count_04
        FROM samples
        """
    ).fetchall()
    updated = 0
    for row in rows:
        raw = json.loads(row["raw_json"])
        variants = raw.get("variants", {}) if isinstance(raw, dict) else {}
        af_counts = variants.get("af_counts", {}) if isinstance(variants, dict) else {}
        values = (
            af_counts.get("0.05"),
            af_counts.get("0.1"),
            af_counts.get("0.15"),
            af_counts.get("0.2"),
            af_counts.get("0.3"),
            af_counts.get("0.4"),
        )
        current = (
            row["variant_af_count_005"],
            row["variant_af_count_01"],
            row["variant_af_count_015"],
            row["variant_af_count_02"],
            row["variant_af_count_03"],
            row["variant_af_count_04"],
        )
        if current == values:
            continue
        connection.execute(
            """
            UPDATE samples
            SET
                variant_af_count_005 = ?,
                variant_af_count_01 = ?,
                variant_af_count_015 = ?,
                variant_af_count_02 = ?,
                variant_af_count_03 = ?,
                variant_af_count_04 = ?
            WHERE sample_run_id = ?
            """,
            (*values, row["sample_run_id"]),
        )
        updated += 1
    connection.commit()
    return updated


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
            sample_run_id, run_name, sequencing_date, generated_date, sample_id, lid, source_root_name, sample_results_relpath,
            typing_report_subtype, typing_main_blast_identity,
            host_filter_reads_in, host_filter_reads_removed_proportion,
            qc_coverage_pct, qc_mean_depth, qc_coverage_1x_pct, qc_coverage_10x_pct,
            qc_coverage_100x_pct, qc_coverage_1000x_pct,
            variant_af_count_005, variant_af_count_01, variant_af_count_015,
            variant_af_count_02, variant_af_count_03, variant_af_count_04,
            sample_metadata_ct, sample_metadata_library_concentration_ng_ul, sample_metadata_library_fragment_length_bp,
            raw_json, imported_at
        )
        VALUES (
            :sample_run_id, :run_name, :sequencing_date, :generated_date, :sample_id, :lid, :source_root_name, :sample_results_relpath,
            :typing_report_subtype, :typing_main_blast_identity,
            :host_filter_reads_in, :host_filter_reads_removed_proportion,
            :qc_coverage_pct, :qc_mean_depth, :qc_coverage_1x_pct, :qc_coverage_10x_pct,
            :qc_coverage_100x_pct, :qc_coverage_1000x_pct,
            :variant_af_count_005, :variant_af_count_01, :variant_af_count_015,
            :variant_af_count_02, :variant_af_count_03, :variant_af_count_04,
            :sample_metadata_ct, :sample_metadata_library_concentration_ng_ul, :sample_metadata_library_fragment_length_bp,
            :raw_json, :imported_at
        )
        ON CONFLICT(sample_run_id) DO UPDATE SET
            run_name = excluded.run_name,
            sequencing_date = excluded.sequencing_date,
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
            variant_af_count_005 = excluded.variant_af_count_005,
            variant_af_count_01 = excluded.variant_af_count_01,
            variant_af_count_015 = excluded.variant_af_count_015,
            variant_af_count_02 = excluded.variant_af_count_02,
            variant_af_count_03 = excluded.variant_af_count_03,
            variant_af_count_04 = excluded.variant_af_count_04,
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


def sync_run_sample_count(connection: sqlite3.Connection, run_name: str) -> None:
    sample_count = connection.execute(
        "SELECT COUNT(*) AS count FROM samples WHERE run_name = ?",
        (run_name,),
    ).fetchone()["count"]
    connection.execute(
        "UPDATE runs SET sample_count = ? WHERE run_name = ?",
        (sample_count, run_name),
    )


def list_runs(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute("SELECT run_name FROM runs ORDER BY run_name DESC").fetchall()
    return [row["run_name"] for row in rows]


def list_subtypes(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT COALESCE(o.value_text, s.typing_report_subtype) AS typing_report_subtype
        FROM samples s
        LEFT JOIN sample_field_overrides o
          ON o.sample_run_id = s.sample_run_id
         AND o.field_name = 'typing_report_subtype'
        WHERE COALESCE(o.value_text, s.typing_report_subtype) IS NOT NULL
          AND COALESCE(o.value_text, s.typing_report_subtype) != ''
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
    sample_categories: list[str] | None = None,
    manual_groups: list[str] | None = None,
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
        clauses.append(
            """
            (
                s.sample_id LIKE ?
                OR COALESCE(s.lid, '') LIKE ?
                OR s.run_name LIKE ?
                OR s.raw_json LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM sample_field_overrides search_overrides
                    WHERE search_overrides.sample_run_id = s.sample_run_id
                      AND search_overrides.value_text LIKE ?
                )
            )
            """.strip()
        )
        needle = f"%{search}%"
        params.extend([needle, needle, needle, needle, needle])
    if run_name:
        clauses.append("s.run_name = ?")
        params.append(run_name)
    if subtype:
        clauses.append(
            """
            COALESCE(
                (
                    SELECT value_text
                    FROM sample_field_overrides subtype_override
                    WHERE subtype_override.sample_run_id = s.sample_run_id
                      AND subtype_override.field_name = 'typing_report_subtype'
                ),
                s.typing_report_subtype
            ) = ?
            """.strip()
        )
        params.append(subtype)
    if qc_status:
        clauses.append("COALESCE(r.qc_status, 'unreviewed') = ?")
        params.append(qc_status)
    if sample_categories:
        configured_categories = [item for item in sample_categories if item not in ("", "__unassigned__")]
        include_unassigned = "__unassigned__" in sample_categories
        category_clauses: list[str] = []
        if configured_categories:
            placeholders = ",".join("?" for _ in configured_categories)
            category_clauses.append(f"a.sample_category IN ({placeholders})")
            params.extend(configured_categories)
        if include_unassigned:
            category_clauses.append("a.sample_category IS NULL")
        if category_clauses:
            clauses.append("(" + " OR ".join(category_clauses) + ")")
    if manual_groups:
        placeholders = ",".join("?" for _ in manual_groups)
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM sample_group_memberships gm_filter
                WHERE gm_filter.sample_run_id = s.sample_run_id
                  AND gm_filter.group_name IN ({placeholders})
            )
            """.strip()
        )
        params.extend(manual_groups)
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
        clauses.append(
            """
            CAST(
                COALESCE(
                    (
                        SELECT value_text
                        FROM sample_field_overrides ct_override
                        WHERE ct_override.sample_run_id = s.sample_run_id
                          AND ct_override.field_name = 'sample_metadata_ct'
                    ),
                    s.sample_metadata_ct
                ) AS REAL
            ) <= ?
            """.strip()
        )
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
            a.sample_category AS sample_category,
            r.updated_at AS qc_updated_at,
            (
                SELECT GROUP_CONCAT(group_name, ', ')
                FROM (
                    SELECT gm.group_name
                    FROM sample_group_memberships gm
                    WHERE gm.sample_run_id = s.sample_run_id
                    ORDER BY gm.group_name ASC
                )
            ) AS manual_groups,
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
        LEFT JOIN sample_annotations a ON a.sample_run_id = s.sample_run_id
        LEFT JOIN sample_comments c ON c.sample_run_id = s.sample_run_id
        {where_sql}
        GROUP BY s.sample_run_id
        ORDER BY {sort_expr} {order}, s.sample_run_id ASC
    """
    rows = [_row_to_dict(row) for row in connection.execute(query, params).fetchall()]
    overrides = _sample_field_overrides_for_rows(connection, [row["sample_run_id"] for row in rows])
    for row in rows:
        _apply_sample_overrides(row, overrides.get(row["sample_run_id"]))

    if sort in OVERRIDABLE_SAMPLE_FIELDS:
        rows.sort(
            key=lambda row: (
                row.get(sort) in (None, ""),
                row.get(sort) if row.get(sort) is not None else "",
                row["sample_run_id"],
            ),
            reverse=desc,
        )
    return rows


def get_sample(connection: sqlite3.Connection, sample_run_id: str) -> dict | None:
    row = connection.execute(
        """
        SELECT
            s.*,
            COALESCE(r.qc_status, 'unreviewed') AS qc_status,
            a.sample_category AS sample_category,
            r.updated_at AS qc_updated_at,
            r.updated_by AS qc_updated_by,
            (
                SELECT GROUP_CONCAT(group_name, ', ')
                FROM (
                    SELECT gm.group_name
                    FROM sample_group_memberships gm
                    WHERE gm.sample_run_id = s.sample_run_id
                    ORDER BY gm.group_name ASC
                )
            ) AS manual_groups
        FROM samples s
        LEFT JOIN sample_review r ON r.sample_run_id = s.sample_run_id
        LEFT JOIN sample_annotations a ON a.sample_run_id = s.sample_run_id
        WHERE s.sample_run_id = ?
        """,
        (sample_run_id,),
    ).fetchone()
    sample = _row_to_dict(row)
    return _apply_sample_overrides(sample, get_sample_field_overrides(connection, sample_run_id))


def get_samples_by_run(connection: sqlite3.Connection, run_name: str) -> list[dict]:
    rows = [
        _row_to_dict(row)
        for row in connection.execute(
            """
            SELECT
                s.*,
                COALESCE(r.qc_status, 'unreviewed') AS qc_status,
                a.sample_category AS sample_category
            FROM samples s
            LEFT JOIN sample_review r ON r.sample_run_id = s.sample_run_id
            LEFT JOIN sample_annotations a ON a.sample_run_id = s.sample_run_id
            WHERE s.run_name = ?
            ORDER BY s.sample_run_id ASC
            """,
            (run_name,),
        ).fetchall()
    ]
    overrides = _sample_field_overrides_for_rows(connection, [row["sample_run_id"] for row in rows])
    for row in rows:
        _apply_sample_overrides(row, overrides.get(row["sample_run_id"]))
    return rows


def list_samples_for_cache_verification(connection: sqlite3.Connection, *, all_runs: bool = False) -> list[dict]:
    where_sql = "WHERE s.run_name != 'manual_failed_samples'" if all_runs else ""
    rows = [
        _row_to_dict(row)
        for row in connection.execute(
            f"""
            SELECT
                s.*,
                COALESCE(r.qc_status, 'unreviewed') AS qc_status,
                a.sample_category AS sample_category
            FROM samples s
            LEFT JOIN sample_review r ON r.sample_run_id = s.sample_run_id
            LEFT JOIN sample_annotations a ON a.sample_run_id = s.sample_run_id
            {where_sql}
            ORDER BY s.run_name DESC, s.sample_run_id ASC
            """
        ).fetchall()
    ]
    overrides = _sample_field_overrides_for_rows(connection, [row["sample_run_id"] for row in rows])
    for row in rows:
        _apply_sample_overrides(row, overrides.get(row["sample_run_id"]))
    return rows


def get_output_cache_entry(connection: sqlite3.Connection, sample_run_id: str, output_key: str) -> dict | None:
    row = connection.execute(
        """
        SELECT sample_run_id, output_key, remote_relpath, cached_relpath, remote_size,
               remote_mtime_ns, cached_sha256, cached_at, verified_at
        FROM output_cache
        WHERE sample_run_id = ? AND output_key = ?
        """,
        (sample_run_id, output_key),
    ).fetchone()
    return _row_to_dict(row)


def upsert_output_cache_entry(connection: sqlite3.Connection, entry: dict) -> None:
    connection.execute(
        """
        INSERT INTO output_cache (
            sample_run_id, output_key, remote_relpath, cached_relpath, remote_size,
            remote_mtime_ns, cached_sha256, cached_at, verified_at
        )
        VALUES (
            :sample_run_id, :output_key, :remote_relpath, :cached_relpath, :remote_size,
            :remote_mtime_ns, :cached_sha256, :cached_at, :verified_at
        )
        ON CONFLICT(sample_run_id, output_key) DO UPDATE SET
            remote_relpath = excluded.remote_relpath,
            cached_relpath = excluded.cached_relpath,
            remote_size = excluded.remote_size,
            remote_mtime_ns = excluded.remote_mtime_ns,
            cached_sha256 = excluded.cached_sha256,
            cached_at = excluded.cached_at,
            verified_at = excluded.verified_at
        """,
        entry,
    )


def delete_output_cache_entry(connection: sqlite3.Connection, sample_run_id: str, output_key: str) -> None:
    connection.execute(
        "DELETE FROM output_cache WHERE sample_run_id = ? AND output_key = ?",
        (sample_run_id, output_key),
    )


def update_output_cache_verified_at(
    connection: sqlite3.Connection,
    sample_run_id: str,
    output_key: str,
    verified_at: str,
) -> None:
    connection.execute(
        """
        UPDATE output_cache
        SET verified_at = ?
        WHERE sample_run_id = ? AND output_key = ?
        """,
        (verified_at, sample_run_id, output_key),
    )


def get_run(connection: sqlite3.Connection, run_name: str) -> dict | None:
    row = connection.execute(
        """
        SELECT run_name, sample_count, pipeline_name, virus, source_root_name, run_relpath, imported_at
        FROM runs
        WHERE run_name = ?
        """,
        (run_name,),
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


def list_stored_sample_categories(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT sample_category
        FROM sample_annotations
        WHERE sample_category IS NOT NULL AND sample_category != ''
        ORDER BY sample_category ASC
        """
    ).fetchall()
    return [row["sample_category"] for row in rows]


def list_manual_groups(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        """
        SELECT DISTINCT group_name
        FROM sample_group_memberships
        WHERE group_name != ''
        ORDER BY group_name ASC
        """
    ).fetchall()
    return [row["group_name"] for row in rows]


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


def set_sample_category(connection: sqlite3.Connection, sample_run_ids: list[str], category: str | None) -> None:
    normalized = (category or "").strip() or None
    for sample_run_id in sample_run_ids:
        if normalized is None:
            connection.execute(
                "DELETE FROM sample_annotations WHERE sample_run_id = ?",
                (sample_run_id,),
            )
            continue
        connection.execute(
            """
            INSERT INTO sample_annotations (sample_run_id, sample_category)
            VALUES (?, ?)
            ON CONFLICT(sample_run_id) DO UPDATE SET
                sample_category = excluded.sample_category
            """,
            (sample_run_id, normalized),
        )
    connection.commit()


def set_sample_field_overrides(
    connection: sqlite3.Connection,
    sample_run_id: str,
    values: dict[str, object],
    *,
    updated_by: str | None = None,
) -> list[dict]:
    imported_row = connection.execute(
        "SELECT * FROM samples WHERE sample_run_id = ?",
        (sample_run_id,),
    ).fetchone()
    if imported_row is None:
        return []

    imported = _row_to_dict(imported_row)
    current_overrides = get_sample_field_overrides(connection, sample_run_id)
    current = _apply_sample_overrides(dict(imported), current_overrides)
    changes = []
    now = utc_now()

    for field_name, raw_value in values.items():
        normalized = _normalize_override_value(field_name, raw_value)
        imported_value = imported.get(field_name)
        current_value = current.get(field_name) if current is not None else imported_value

        if normalized is None or _typed_override_equal(field_name, normalized, imported_value):
            if field_name not in current_overrides:
                continue
            connection.execute(
                "DELETE FROM sample_field_overrides WHERE sample_run_id = ? AND field_name = ?",
                (sample_run_id, field_name),
            )
            changes.append(
                {
                    "field_name": field_name,
                    "old_value": current_value,
                    "new_value": imported_value,
                    "cleared": True,
                }
            )
            continue

        if field_name in current_overrides and _typed_override_equal(field_name, normalized, current_overrides[field_name]):
            continue

        connection.execute(
            """
            INSERT INTO sample_field_overrides (sample_run_id, field_name, value_text, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sample_run_id, field_name) DO UPDATE SET
                value_text = excluded.value_text,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (sample_run_id, field_name, normalized, now, updated_by),
        )
        changes.append(
            {
                "field_name": field_name,
                "old_value": current_value,
                "new_value": _coerce_override_value(field_name, normalized),
                "cleared": False,
            }
        )

    connection.commit()
    return changes


def add_samples_to_group(connection: sqlite3.Connection, sample_run_ids: list[str], group_name: str) -> None:
    normalized = group_name.strip()
    for sample_run_id in sample_run_ids:
        connection.execute(
            """
            INSERT INTO sample_group_memberships (sample_run_id, group_name)
            VALUES (?, ?)
            ON CONFLICT(sample_run_id, group_name) DO NOTHING
            """,
            (sample_run_id, normalized),
        )
    connection.commit()


def remove_samples_from_group(connection: sqlite3.Connection, sample_run_ids: list[str], group_name: str) -> None:
    normalized = group_name.strip()
    if not normalized or not sample_run_ids:
        return
    connection.execute(
        f"""
        DELETE FROM sample_group_memberships
        WHERE group_name = ?
          AND sample_run_id IN ({",".join("?" for _ in sample_run_ids)})
        """,
        [normalized, *sample_run_ids],
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
