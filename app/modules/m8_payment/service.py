"""M8 付款链路收敛编排：幂等重算、原子切换、断点边复核决定。

- 收敛结果按"代"落库，只有校验通过才一次性切换指针；失败保留旧路径。
- 同一批数据重算得到同一份结果时不切换、不新增行，待办不重复。
- 断点边决定（verify/hold）带版本乐观锁，写入后立即重算收敛；
  已核（verify）路径由决定表驱动，重跑不会被改变。
"""
from __future__ import annotations

import json
from decimal import Decimal

from ... import store
from ...errors import Conflict, DomainError, NotFound
from ...manifest import utc_iso, utc_now
from ...reasons import (
    V_BAD_DECISION,
    V_NO_PENDING_EDGE,
    V_STALE_DECISION,
    V_UNKNOWN_RECORD,
    V_UNKNOWN_RUN,
)
from ..m5_clusters import service as cluster_service
from . import chain


def _canonical(rows) -> list:
    def norm(row) -> tuple:
        chain_raw = row["chain"] if "chain" in row else json.loads(row["chain_json"] or "[]")
        return (row["supplier_id"], row["status"], row["payee_id"], row["payee_cluster_id"],
                json.dumps(chain_raw, ensure_ascii=False, sort_keys=True),
                row["reason_code"], row["detail"], row["edge_key"])
    return sorted(norm(row) for row in rows)


def ensure_resolutions(run_id: str) -> dict:
    """重算收敛并原子切换；内容相同则复用当前代，不重复生成待办。"""
    cluster_service.ensure_clusters(run_id)
    records = store.list_records(run_id)
    if not records:
        raise NotFound(V_UNKNOWN_RUN, run_id)
    clusters = store.list_clusters(run_id)
    members = store.list_cluster_members(run_id)
    decisions = {item["edge_key"]: item for item in store.list_ref_decisions(run_id)}
    rows = chain.resolve_all(records, clusters, members, decisions)
    stored = store.list_resolutions(run_id)
    if stored and _canonical(stored) == _canonical(rows):
        return summary(run_id, switched=False)
    payload = [{**row, "chain_json": json.dumps(row["chain"], ensure_ascii=False)}
               for row in rows]
    store.switch_resolutions(run_id, payload, utc_iso(utc_now()))
    return summary(run_id, switched=True)


def _edge_states(run_id: str) -> dict:
    return {item["edge_key"]: item for item in store.list_ref_decisions(run_id)}


def payment_list(run_id: str) -> list:
    """付款清单：收款主体、链路证据与未决原因，与导出共用同一生效代。"""
    ensure_resolutions(run_id)
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    edges = _edge_states(run_id)
    items = []
    for row in store.list_resolutions(run_id):
        record = records.get(row["supplier_id"], {})
        payee = records.get(row["payee_id"], {})
        edge = edges.get(row["edge_key"]) if row["edge_key"] else None
        items.append({
            "supplier_id": row["supplier_id"],
            "supplier_no": record.get("supplier_no", ""),
            "raw_name": record.get("raw_name", ""),
            "ref_master_id": record.get("ref_master_id", ""),
            "ytd_amount": record.get("ytd_amount", "0.00"),
            "status": row["status"],
            "status_label": chain.STATUS_LABELS[row["status"]],
            "payee_id": row["payee_id"],
            "payee_name": payee.get("raw_name", ""),
            "payee_tax": payee.get("tax_id", ""),
            "payee_cluster_id": row["payee_cluster_id"],
            "chain": json.loads(row["chain_json"] or "[]"),
            "reason_code": row["reason_code"],
            "detail": row["detail"],
            "edge_key": row["edge_key"],
            "edge_version": edge["version"] if edge else 0,
            "edge_decision": edge["decision"] if edge else "",
        })
    order = {chain.STATUS_HALTED: 0, chain.STATUS_SETTLED: 1,
             chain.STATUS_RESOLVED: 2, chain.STATUS_DIRECT: 3}
    return sorted(items, key=lambda item: (order[item["status"]], item["supplier_id"]))


def summary(run_id: str, switched: bool = False) -> dict:
    rows = store.list_resolutions(run_id)
    records = {record["supplier_id"]: record for record in store.list_records(run_id)}
    halted_amount = sum((Decimal(records[row["supplier_id"]]["ytd_amount"])
                         for row in rows if row["status"] == chain.STATUS_HALTED
                         and row["supplier_id"] in records), Decimal("0"))
    stats = {status: sum(1 for row in rows if row["status"] == status)
             for status in chain.STATUSES}
    return {
        "run_id": run_id,
        "generation": store.active_generation(run_id),
        "switched": switched,
        "total": len(rows),
        **stats,
        "todos": sum(1 for row in rows
                     if row["status"] == chain.STATUS_HALTED and row["edge_key"]),
        "halted_amount": str(halted_amount),
    }


def decide(run_id: str, supplier_id: str, decision: str, expected_version: int,
           operator: str = "采购主数据组") -> dict:
    """对断点边作 verify/hold 决定，版本乐观锁，写入后立即重算收敛。

    挂起（halted）与已落定（settled）记录都允许提交：重复提交旧版本
    一律 V_STALE_DECISION；已落定记录可按新版本改判 hold 撤销落定。
    """
    if decision not in chain.DECIDE_ACTIONS:
        raise DomainError(V_BAD_DECISION, decision)
    if store.get_record(run_id, supplier_id) is None:
        raise NotFound(V_UNKNOWN_RECORD, supplier_id)
    ensure_resolutions(run_id)
    current = {row["supplier_id"]: row
               for row in store.list_resolutions(run_id)}.get(supplier_id)
    if current is None or not current["edge_key"] \
            or current["status"] not in (chain.STATUS_HALTED, chain.STATUS_SETTLED):
        raise DomainError(V_NO_PENDING_EDGE, supplier_id)
    edge_key = current["edge_key"]
    existing = store.get_ref_decision(run_id, edge_key)
    version = existing["version"] if existing else 0
    if expected_version != version:
        raise Conflict(V_STALE_DECISION, f"当前版本 {version}")
    record = {
        "run_id": run_id,
        "edge_key": edge_key,
        "decision": decision,
        "expected_version": expected_version,
        "version": version + 1,
        "operator": operator,
        "decided_at": utc_iso(utc_now()),
    }
    store.save_ref_decision(record)
    ensure_resolutions(run_id)
    return record
