from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from virtitta.artifact_cache import _copy_with_sha256


def test_concurrent_cache_copies_never_publish_partial_content():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        first = root / "first"
        second = root / "second"
        destination = root / "cache" / "artifact"
        first_content = b"A" * (1024 * 1024)
        second_content = b"B" * (1024 * 1024)
        first.write_bytes(first_content)
        second.write_bytes(second_content)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda source: _copy_with_sha256(source, destination), (first, second)))

        assert len(set(results)) == 2
        assert destination.read_bytes() in {first_content, second_content}
        assert list(destination.parent.glob("*.tmp")) == []
