from __future__ import annotations

from pathlib import Path

from kb.parsers import parse_file
from kb.scanner import scan_inputs
from kb.storage import Workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_parse_txt_and_csv_samples(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    scan = scan_inputs([PROJECT_ROOT / "samples"], workspace)

    assert len(scan.files) == 2
    parsed = [parse_file(workspace, item) for item in scan.files]

    txt = next(item for item in parsed if item.document.file_name.endswith(".txt"))
    csv_file = next(item for item in parsed if item.document.file_name.endswith(".csv"))

    assert len(txt.blocks) == 4
    assert any("第2.1条" in block.text for block in txt.blocks)
    assert any("\n" in block.text for block in txt.blocks)
    assert len(csv_file.tables) == 4
    assert csv_file.tables[0].columns["设备名称"] == "给水泵"
    assert csv_file.tables[0].columns["上限"] == "1.2"


def test_scan_deduplicates_same_content(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("重复内容", encoding="utf-8")
    second.write_text("重复内容", encoding="utf-8")
    workspace = Workspace(tmp_path / "workspace")

    result = scan_inputs([first, second], workspace)

    assert len(result.files) == 1
    assert len(result.skipped_duplicates) == 1
