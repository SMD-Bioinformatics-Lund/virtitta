from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from virtitta.config import Config
from virtitta.outputs import effective_output_relname, safe_relative_path
from virtitta.repository import (
    delete_output_cache_entry,
    get_output_cache_entry,
    raw_json_for_sample,
    update_output_cache_verified_at,
    upsert_output_cache_entry,
    utc_now,
)


CACHE_OK = "ok"
CACHE_STALE = "stale"
CACHE_MISSING_CACHE = "missing-cache"
CACHE_MISSING_REMOTE = "missing-remote"
CACHE_UNCONFIGURED = "unconfigured"
CACHE_CURRENT = "cache-current"
CACHE_OFFLINE_UNVERIFIED = "cache-offline-unverified"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedArtifact:
    path: Path
    source: str


def is_cacheable_output(config: Config, output_key: str) -> bool:
    return output_key in config.cache.output_keys


def safe_output_path(sample_dir: Path, relname: str) -> Path:
    try:
        return safe_relative_path(sample_dir, relname)
    except ValueError as exc:
        raise ValueError("Unsafe file path") from exc


def remote_output_path(config: Config, sample_row: dict, output_key: str) -> tuple[Path, str] | None:
    raw = raw_json_for_sample(sample_row)
    outputs = raw.get("outputs", {}) if isinstance(raw, dict) else {}
    relname = effective_output_relname(output_key, outputs)
    if not relname:
        return None

    root = config.get_root(sample_row["source_root_name"])
    if root is None:
        return None
    sample_dir = root.linux_path / sample_row["sample_results_relpath"]
    return safe_output_path(sample_dir, str(relname)), str(relname)


def cached_file_path(config: Config, cached_relpath: str) -> Path:
    relpath = Path(cached_relpath)
    if relpath.is_absolute() or ".." in relpath.parts:
        raise ValueError("Unsafe cached file path")
    return config.cache.outputs_root / relpath


def resolve_cached_output(config: Config, connection, sample_row: dict, output_key: str) -> ResolvedArtifact | None:
    if not is_cacheable_output(config, output_key):
        return None

    entry = get_output_cache_entry(connection, sample_row["sample_run_id"], output_key)
    cached_path = cached_file_path(config, entry["cached_relpath"]) if entry is not None else None
    cached_exists = cached_path is not None and cached_path.is_file()
    resolved = remote_output_path(config, sample_row, output_key)
    remote_path, relname = resolved if resolved is not None else (None, None)
    try:
        remote_stat = remote_path.stat() if remote_path is not None else None
        remote_exists = remote_path is not None and remote_path.is_file()
    except OSError:
        remote_stat = None
        remote_exists = False

    if remote_exists and remote_stat is not None:
        current = (
            entry is not None
            and cached_exists
            and relname == entry["remote_relpath"]
            and remote_stat.st_size == entry["remote_size"]
            and remote_stat.st_mtime_ns == entry["remote_mtime_ns"]
        )
        if current:
            return ResolvedArtifact(cached_path, CACHE_CURRENT)
        try:
            refreshed = _cache_output(config, connection, sample_row, output_key, remote_path, relname, remote_stat)
        except OSError:
            try:
                still_available = remote_path.is_file()
            except OSError:
                still_available = False
            if still_available or not cached_exists:
                raise
            logger.warning(
                "Serving unverified cached artifact after source became unavailable: sample_run_id=%s output_key=%s source=%s",
                sample_row["sample_run_id"], output_key, remote_path,
            )
            return ResolvedArtifact(cached_path, CACHE_OFFLINE_UNVERIFIED)
        connection.commit()
        logger.info(
            "Refreshed stale cached artifact: sample_run_id=%s output_key=%s source=%s",
            sample_row["sample_run_id"], output_key, remote_path,
        )
        return ResolvedArtifact(refreshed, CACHE_CURRENT)

    if cached_exists:
        logger.warning(
            "Serving unverified cached artifact because source is unavailable: sample_run_id=%s output_key=%s source=%s",
            sample_row["sample_run_id"], output_key, remote_path,
        )
        return ResolvedArtifact(cached_path, CACHE_OFFLINE_UNVERIFIED)
    return None


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return normalized or "artifact"


def _cache_relpath(sample_run_id: str, output_key: str, relname: str) -> Path:
    return Path(_safe_name(output_key)) / _safe_name(sample_run_id) / _safe_name(Path(relname).name)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _delete_cache_entry_and_file(config: Config, connection, sample_run_id: str, output_key: str) -> None:
    entry = get_output_cache_entry(connection, sample_run_id, output_key)
    delete_output_cache_entry(connection, sample_run_id, output_key)
    if entry is None:
        return
    path = cached_file_path(config, entry["cached_relpath"])
    if path.exists():
        path.unlink()


def _copy_with_sha256(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        with source.open("rb") as source_handle, temp_path.open("wb") as destination_handle:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                digest.update(chunk)
                destination_handle.write(chunk)
        temp_path.replace(destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return digest.hexdigest()


def _cache_output(config, connection, sample_row, output_key, remote_path, relname, remote_stat) -> Path:
    cached_relpath = _cache_relpath(sample_row["sample_run_id"], output_key, relname)
    destination = config.cache.outputs_root / cached_relpath
    digest = _copy_with_sha256(remote_path, destination)
    previous_entry = get_output_cache_entry(connection, sample_row["sample_run_id"], output_key)
    upsert_output_cache_entry(
        connection,
        {
            "sample_run_id": sample_row["sample_run_id"],
            "output_key": output_key,
            "remote_relpath": relname,
            "cached_relpath": cached_relpath.as_posix(),
            "remote_size": remote_stat.st_size,
            "remote_mtime_ns": remote_stat.st_mtime_ns,
            "cached_sha256": digest,
            "cached_at": utc_now(),
            "verified_at": None,
        },
    )
    if previous_entry is not None and previous_entry["cached_relpath"] != cached_relpath.as_posix():
        previous_path = cached_file_path(config, previous_entry["cached_relpath"])
        if previous_path.exists():
            previous_path.unlink()
    return destination


def cache_sample_outputs(config: Config, connection, sample_row: dict) -> int:
    cached = 0
    for output_key in config.cache.output_keys:
        resolved = remote_output_path(config, sample_row, output_key)
        if resolved is None:
            _delete_cache_entry_and_file(config, connection, sample_row["sample_run_id"], output_key)
            continue
        remote_path, relname = resolved
        if not remote_path.is_file():
            _delete_cache_entry_and_file(config, connection, sample_row["sample_run_id"], output_key)
            continue

        stat = remote_path.stat()
        _cache_output(config, connection, sample_row, output_key, remote_path, relname, stat)
        cached += 1
    return cached


def verify_cached_output(config: Config, connection, sample_row: dict, output_key: str) -> dict:
    if not is_cacheable_output(config, output_key):
        return _verification_result(sample_row, output_key, CACHE_UNCONFIGURED)

    resolved = remote_output_path(config, sample_row, output_key)
    if resolved is None:
        return _verification_result(sample_row, output_key, CACHE_MISSING_REMOTE)
    remote_path, relname = resolved
    if not remote_path.is_file():
        return _verification_result(sample_row, output_key, CACHE_MISSING_REMOTE)

    entry = get_output_cache_entry(connection, sample_row["sample_run_id"], output_key)
    if entry is None:
        return _verification_result(sample_row, output_key, CACHE_MISSING_CACHE)

    cached_path = cached_file_path(config, entry["cached_relpath"])
    if not cached_path.is_file():
        return _verification_result(sample_row, output_key, CACHE_MISSING_CACHE)

    remote_stat = remote_path.stat()
    remote_sha256 = _sha256_file(remote_path)
    cached_sha256 = _sha256_file(cached_path)
    status = CACHE_OK
    if (
        relname != entry["remote_relpath"]
        or remote_stat.st_size != entry["remote_size"]
        or remote_stat.st_mtime_ns != entry["remote_mtime_ns"]
        or remote_sha256 != entry["cached_sha256"]
        or cached_sha256 != entry["cached_sha256"]
    ):
        status = CACHE_STALE

    if status == CACHE_OK:
        verified_at = utc_now()
        update_output_cache_verified_at(connection, sample_row["sample_run_id"], output_key, verified_at)
        connection.commit()
        return _verification_result(sample_row, output_key, status, verified_at=verified_at)
    return _verification_result(sample_row, output_key, status)


def verify_sample_cache(config: Config, connection, sample_row: dict) -> list[dict]:
    return [
        verify_cached_output(config, connection, sample_row, output_key)
        for output_key in config.cache.output_keys
    ]


def _verification_result(
    sample_row: dict,
    output_key: str,
    status: str,
    *,
    verified_at: str | None = None,
) -> dict:
    return {
        "sample_run_id": sample_row["sample_run_id"],
        "run_name": sample_row["run_name"],
        "output_key": output_key,
        "status": status,
        "verified_at": verified_at,
    }
