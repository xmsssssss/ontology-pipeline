from __future__ import annotations

from kb.extraction import extract_all, extract_entities
from kb.models import ClauseRecord, DocumentRecord


def test_entity_extraction_trims_role_and_action_prefixes():
    entities = extract_entities("给水泵启动前，值班人员必须检查润滑油系统和出口阀门状态。")
    labels = {item[0] for item in entities}

    assert "给水泵" in labels
    assert "润滑油系统" in labels
    assert "出口阀门" in labels
    assert all("值班人员" not in item for item in labels)
    assert extract_entities("操作人员可以执行紧急停泵。") == []


def test_extract_threshold_and_prohibition():
    document = DocumentRecord(
        document_id="doc_test",
        file_id="file_test",
        title="运行规程样例",
        file_name="运行规程样例.txt",
        file_type="unstructured",
    )
    clauses = [
        ClauseRecord(
            clause_id="clause_limit",
            document_id=document.document_id,
            clause_no="第3.1条",
            text="给水泵轴承温度不应超过75摄氏度，振动值不应超过4.5mm/s。",
        ),
        ClauseRecord(
            clause_id="clause_action",
            document_id=document.document_id,
            clause_no="第2.3条",
            text="严禁在未关闭出口阀门的情况下拆卸泵体。",
        ),
    ]

    result = extract_all([document], clauses, [])
    predicates = {item.predicate for item in result.facts}
    upper_bounds = [item for item in result.facts if item.predicate == "hasUpperBound"]

    assert "hasUpperBound" in predicates
    assert "prohibitsAction" in predicates
    assert {item.object for item in upper_bounds} == {"75", "4.5"}
    assert not [item for item in result.facts if item.fact_type == "parameter_condition"]
    assert all(item.evidence_text for item in result.facts)


def test_action_uses_document_equipment_and_extracts_conditions():
    document = DocumentRecord(
        document_id="doc_actions",
        file_id="file_actions",
        title="给水泵运行规程",
        file_name="给水泵运行规程.txt",
        file_type="unstructured",
    )
    clauses = [
        ClauseRecord(
            clause_id="clause_start",
            document_id=document.document_id,
            clause_no="第2.1条",
            text="给水泵启动前，值班人员必须检查润滑油系统和出口阀门状态。",
        ),
        ClauseRecord(
            clause_id="clause_valve",
            document_id=document.document_id,
            clause_no="第2.3条",
            text="严禁在未关闭出口阀门的情况下拆卸泵体。",
        ),
        ClauseRecord(
            clause_id="clause_stop",
            document_id=document.document_id,
            clause_no="第2.4条",
            text="设备异常时，操作人员可以执行紧急停泵，并记录故障现象。",
        ),
    ]

    result = extract_all([document], clauses, [])
    actions = {
        item.clause_no: item for item in result.facts if item.fact_type == "regulatory_action"
    }

    assert actions["第2.1条"].subject == "给水泵"
    assert actions["第2.1条"].condition == "给水泵启动前"
    assert actions["第2.3条"].subject == "给水泵"
    assert actions["第2.3条"].condition == "在未关闭出口阀门的情况下"
    assert actions["第2.3条"].object == "拆卸泵体"
    assert actions["第2.4条"].subject == "给水泵"
    assert actions["第2.4条"].condition == "设备异常时"


def test_evidence_and_action_stop_at_sentence_boundaries():
    document = DocumentRecord(
        document_id="doc_long",
        file_id="file_long",
        title="给水泵运行规程",
        file_name="给水泵运行规程.txt",
        file_type="unstructured",
    )
    clause = ClauseRecord(
        clause_id="clause_long",
        document_id=document.document_id,
        clause_no="第1条",
        text=(
            "给水泵启动前，值班人员必须检查润滑油系统。"
            "运行中应监测轴承温度。"
            "设备异常时，操作人员可以执行紧急停泵，并记录故障现象。"
        ),
    )

    result = extract_all([document], [clause], [])
    action = next(item for item in result.facts if item.fact_type == "regulatory_action")

    assert action.object == "检查润滑油系统"
    assert action.evidence_text == "给水泵启动前，值班人员必须检查润滑油系统。"
    assert all(len(item.evidence_text) < len(clause.text) for item in result.facts)
