"""M8 付款链路收敛：穿透与停核判定、幂等重跑、原子切换、导出同源。"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

import pytest

from app import store
from app.errors import Conflict, DomainError, NotFound
from app.modules.m1_intake import service as intake_service
from app.modules.m8_payment import payment_out, service
from tests.fixtures import expected as EXP


def _items(run_id: str) -> dict:
    return {item["supplier_id"]: item for item in service.payment_list(run_id)}


def test_sample_resolution_snapshot(run):
    stats = service.ensure_resolutions(run["run_id"])
    assert stats["total"] == EXP.ACCEPTED
    assert stats["direct"] == 40
    assert stats["resolved"] == 22
    assert stats["halted"] == 8
    assert stats["settled"] == 0
    assert stats["todos"] == 4
    assert stats["generation"] == 1
    assert stats["switched"] is True


def test_multi_hop_chain_converges(run):
    """A 指 B、B 再指 C：链上每跳有效即可穿透到最终主档，不被中间级拦下。"""
    item = _items(run["run_id"])["S-0067"]
    assert item["status"] == "resolved"
    assert item["payee_id"] == "S-0015"
    assert item["payee_cluster_id"]
    assert item["chain"] == [{"from": "S-0067", "to": "S-0033"},
                             {"from": "S-0033", "to": "S-0015"}]
    assert item["reason_code"] == ""


def test_converge_to_cluster_representative(run):
    """链中主体已与其他记录合并：收敛终点是末端所属簇的代表元。"""
    item = _items(run["run_id"])["S-0021"]
    assert item["status"] == "resolved"
    assert item["payee_id"] == "S-0003"
    assert item["payee_tax"] == "91310115MA1H8K2Q3"
    direct = _items(run["run_id"])["S-0001"]
    assert direct["status"] == "direct"
    assert direct["payee_id"] == "S-0001"


def test_halt_at_last_verified_node(run):
    """断链、自引、成环：停在最后已核节点，未决原因与断点边可查。"""
    items = _items(run["run_id"])
    unknown = items["S-0053"]
    assert unknown["status"] == "halted"
    assert unknown["reason_code"] == "X_REF_UNKNOWN"
    assert unknown["edge_key"] == "S-0053->S-0099"
    assert unknown["payee_id"] == "S-0003"  # 最后已核节点 S-0053 的簇代表元
    self_ref = items["S-0064"]
    assert self_ref["reason_code"] == "X_REF_SELF"
    assert self_ref["payee_id"] == "S-0041"
    for sid, edge in (("S-0065", "S-0066->S-0065"), ("S-0066", "S-0065->S-0066")):
        cycle = items[sid]
        assert cycle["status"] == "halted"
        assert cycle["reason_code"] == "X_REF_CYCLE"
        assert cycle["edge_key"] == edge
        assert cycle["payee_id"] == "S-0016"


def test_conflict_cluster_halts(run):
    """落点簇税号冲突未决：挂起且无已核节点，待办不生成边决定项。"""
    items = _items(run["run_id"])
    for sid in ("S-0028", "S-0010", "S-0071", "S-0072"):
        item = items[sid]
        assert item["status"] == "halted"
        assert item["reason_code"] == "C_CLUSTER_CONFLICT"
        assert item["payee_id"] == ""
        assert item["edge_key"] == ""


def test_verify_settles_and_survives_rerun(run):
    """人工确认断点边后落定在最后已核节点；同批重跑不改已核路径。"""
    run_id = run["run_id"]
    record = service.decide(run_id, "S-0053", "verify", 0, "王核")
    assert record["edge_key"] == "S-0053->S-0099"
    assert record["version"] == 1
    item = _items(run_id)["S-0053"]
    assert item["status"] == "settled"
    assert item["payee_id"] == "S-0003"
    assert item["reason_code"] == "X_REF_UNKNOWN"  # 原断点原因保留作证据
    intake_service.ingest(force=True)  # 同批重跑
    again = _items(run_id)["S-0053"]
    assert again["status"] == "settled"
    assert again["payee_id"] == "S-0003"
    assert store.get_ref_decision(run_id, "S-0053->S-0099")["decision"] == "verify"


def test_hold_then_verify(run):
    run_id = run["run_id"]
    held = service.decide(run_id, "S-0064", "hold", 0)
    assert held["version"] == 1
    assert _items(run_id)["S-0064"]["status"] == "halted"
    settled = service.decide(run_id, "S-0064", "verify", 1)
    assert settled["version"] == 2
    assert _items(run_id)["S-0064"]["status"] == "settled"
    assert _items(run_id)["S-0064"]["payee_id"] == "S-0041"


def test_settled_can_be_reverted(run):
    """已落定记录允许按新版本改判 hold，撤销后回到挂起待核。"""
    run_id = run["run_id"]
    service.decide(run_id, "S-0053", "verify", 0)
    assert _items(run_id)["S-0053"]["status"] == "settled"
    reverted = service.decide(run_id, "S-0053", "hold", 1)
    assert reverted["version"] == 2
    item = _items(run_id)["S-0053"]
    assert item["status"] == "halted"
    assert item["payee_id"] == "S-0003"


def test_stale_edge_decision_rejected(run):
    run_id = run["run_id"]
    service.decide(run_id, "S-0053", "verify", 0)
    with pytest.raises(Conflict) as error:
        service.decide(run_id, "S-0053", "verify", 0)
    assert error.value.code == "V_STALE_DECISION"


def test_decide_guards(run):
    run_id = run["run_id"]
    with pytest.raises(NotFound) as unknown:
        service.decide(run_id, "S-9999", "verify", 0)
    assert unknown.value.code == "V_UNKNOWN_RECORD"
    with pytest.raises(DomainError) as no_edge:
        service.decide(run_id, "S-0001", "verify", 0)  # 直付记录无断点
    assert no_edge.value.code == "V_NO_PENDING_EDGE"
    with pytest.raises(DomainError) as bad:
        service.decide(run_id, "S-0053", "drop", 0)
    assert bad.value.code == "V_BAD_DECISION"


def test_idempotent_rerun_no_duplicate_todos(run):
    """同批重算：不切换代、不新增行、待办不重复。"""
    run_id = run["run_id"]
    first = service.ensure_resolutions(run_id)
    rows_before = store.list_resolutions(run_id)
    second = service.ensure_resolutions(run_id)
    assert second["switched"] is False
    assert second["generation"] == first["generation"]
    assert store.list_resolutions(run_id) == rows_before
    assert len(rows_before) == EXP.ACCEPTED
    assert second["todos"] == first["todos"] == 4
    assert store.count(run_id, "ref_decisions") == 0


def test_atomic_switch_keeps_old_on_failure(run):
    """校验失败时保留旧路径：指针不动、旧代内容不变。"""
    run_id = run["run_id"]
    service.ensure_resolutions(run_id)
    generation = store.active_generation(run_id)
    rows_before = store.list_resolutions(run_id)
    bad = [dict(row) for row in rows_before]
    bad[0] = {**bad[0], "status": "resolved", "payee_id": "S-9999"}
    with pytest.raises(DomainError) as error:
        store.switch_resolutions(run_id, bad, "2026-11-17T00:00:00Z")
    assert error.value.code == "R_SWITCH_INVALID"
    assert store.active_generation(run_id) == generation
    assert store.list_resolutions(run_id) == rows_before
    with pytest.raises(DomainError):
        store.switch_resolutions(run_id, rows_before[:-1], "2026-11-17T00:00:00Z")
    assert store.active_generation(run_id) == generation


def test_export_matches_page(run):
    """导出与页面只能采用同一收敛结果：同一代、同一收款主体。"""
    run_id = run["run_id"]
    items = service.payment_list(run_id)
    rows = list(csv.reader(io.StringIO(payment_out.render(items))))
    assert rows[0][0] == "供应商号"
    assert rows[0][4] == "收款主体"
    assert len(rows) - 1 == EXP.ACCEPTED
    body = {row[0]: row for row in rows[1:]}
    assert body["S-0067"][4] == "S-0015"
    assert body["S-0067"][8] == "S-0067 -> S-0033 -> S-0015"
    assert body["S-0053"][3] == "挂起待核"
    assert body["S-0053"][9] == "X_REF_UNKNOWN"
    by_id = {item["supplier_id"]: item for item in items}
    for supplier_id, row in body.items():
        assert row[4] == by_id[supplier_id]["payee_id"]


def test_payment_amount_conservation(run):
    """每条记录恰一行解析，挂起金额与总金额对账一致。"""
    run_id = run["run_id"]
    stats = service.ensure_resolutions(run_id)
    items = service.payment_list(run_id)
    total = sum((Decimal(item["ytd_amount"]) for item in items), Decimal("0"))
    assert total == EXP.AMOUNT_TOTAL
    halted = sum((Decimal(item["ytd_amount"]) for item in items
                  if item["status"] == "halted"), Decimal("0"))
    assert str(halted) == stats["halted_amount"]


def test_payment_api_and_page(client):
    response = client.get("/api/payment")
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["halted"] == 8
    assert body["stats"]["todos"] == 4
    assert body["total"] == EXP.ACCEPTED

    page = client.get("/payment")
    assert page.status_code == 200
    assert "付款链路" in page.text
    assert "挂起待核" in page.text
    assert "S-0067" in page.text

    export = client.get("/api/export/payment.csv")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert export.text.startswith("﻿供应商号")

    decide = client.post("/api/payment/S-0053/decision",
                         json={"decision": "verify", "expected_version": 0})
    assert decide.status_code == 200
    assert decide.json()["decision"]["version"] == 1
    stale = client.post("/api/payment/S-0053/decision",
                        json={"decision": "verify", "expected_version": 0})
    assert stale.status_code == 409
    assert stale.json()["reason_code"] == "V_STALE_DECISION"
    rerun = client.post("/api/payment/resolve")
    assert rerun.status_code == 200
    assert rerun.json()["switched"] is False
    assert rerun.json()["settled"] == 1
