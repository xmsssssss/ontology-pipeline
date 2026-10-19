from __future__ import annotations

import hashlib

from kb.ids import sha256_file, stable_id
from kb.storage import Workspace


def test_sha256_and_stable_id(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("same content", encoding="utf-8")
    second.write_text("same content", encoding="utf-8")

    assert sha256_file(first) == sha256_file(second)
    assert sha256_file(first) == hashlib.sha256(b"same content").hexdigest()
    assert stable_id("x", "A", "B") == stable_id("x", " A ", "B")


def test_checksums_cover_artifacts(tmp_path):
    workspace = Workspace(tmp_path).ensure()
    workspace.atomic_write_text(tmp_path / "ontology.ttl", "triple")
    workspace.atomic_write_text(tmp_path / "manifest.json", "{}")

    checksum_path = workspace.write_checksums(tmp_path)
    lines = checksum_path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert any(line.endswith("ontology.ttl") for line in lines)
    assert any(line.endswith("manifest.json") for line in lines)
