"""Content-addressed file storage on a shared volume.

The path comes only from the tenant id and the hash, so a client's filename never touches
the filesystem. A file is complete before any row refers to it, so a crash can leave an
unreferenced file but never a row pointing at nothing.
"""

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from mailroom.config import settings


def store(stream: BinaryIO, tenant_id: uuid.UUID, content_hash: str) -> str:
    relative_path = f"{tenant_id}/{content_hash}"
    final_path = Path(settings.storage_dir) / relative_path
    if final_path.exists():
        return relative_path

    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=final_path.parent, delete=False) as temporary:
        stream.seek(0)
        shutil.copyfileobj(stream, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
    # Rename is atomic, and concurrent writers of the same hash hold identical bytes.
    os.replace(temporary.name, final_path)
    directory = os.open(final_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return relative_path


def read(relative_path: str) -> bytes:
    return (Path(settings.storage_dir) / relative_path).read_bytes()
