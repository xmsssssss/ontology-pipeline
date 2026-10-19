from __future__ import annotations

from kb.models import BlockRecord, DocumentRecord
from kb.segmentation import segment_document


def test_segment_clauses_and_chapters():
    document = DocumentRecord(
        document_id="doc_test",
        file_id="file_test",
        title="运行规程",
        file_name="运行规程.txt",
        file_type="unstructured",
    )
    block = BlockRecord(
        block_id="block_test",
        document_id=document.document_id,
        order=0,
        block_type="paragraph",
        text=(
            "第一章 总则\n"
            "第1.1条 本规程适用于给水泵。\n"
            "第1.2条 值班人员应熟悉出口压力。\n"
            "第二章 运行要求\n"
            "第2.1条 给水泵启动前必须检查润滑油系统。"
        ),
    )

    clauses = segment_document(document, [block])

    assert [item.clause_no for item in clauses] == ["第1.1条", "第1.2条", "第2.1条"]
    assert clauses[0].chapter_title == "第一章 总则"
    assert clauses[2].chapter_title == "第二章 运行要求"
    assert clauses[2].text == "给水泵启动前必须检查润滑油系统。"


def test_unstructured_blocks_without_clause_numbers_are_not_merged():
    document = DocumentRecord(
        document_id="doc_plain",
        file_id="file_plain",
        title="设备说明书",
        file_name="设备说明书.txt",
        file_type="unstructured",
    )
    blocks = [
        BlockRecord(
            block_id="block_1",
            document_id=document.document_id,
            order=0,
            block_type="paragraph",
            text="给水泵启动前应检查润滑油系统。",
        ),
        BlockRecord(
            block_id="block_2",
            document_id=document.document_id,
            order=1,
            block_type="paragraph",
            text="运行中应监测轴承温度和振动值。",
        ),
    ]

    clauses = segment_document(document, blocks)

    assert len(clauses) == 2
    assert [item.text for item in clauses] == [
        "给水泵启动前应检查润滑油系统。",
        "运行中应监测轴承温度和振动值。",
    ]
