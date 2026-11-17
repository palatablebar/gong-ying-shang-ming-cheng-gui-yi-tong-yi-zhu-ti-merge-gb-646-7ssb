"""付款链路收敛：引用链逐跳核验，判定收敛终点或最后已核节点。

收敛规则（纯函数，不碰数据库）：
- 沿 ref_master_id 逐跳走，目标存在、非自引、不成环即可穿透；
- 走到无引用的末端节点，且末端所属合并簇无未决税号冲突，
  收敛到末端簇代表元（最终主档），状态 resolved（无引用记录为 direct）；
- 链上第一处断点（断链 X_REF_UNKNOWN、自引 X_REF_SELF、成环 X_REF_CYCLE）
  或落点簇税号冲突未决（C_CLUSTER_CONFLICT）时，停在最后已核节点
  ——从断点沿路径回退的第一个无冲突簇节点，状态 halted，生成待办；
- 人工对断点边 verify 后该边已核，落定在最后已核节点（settled），
  重跑不再改变；hold 维持挂起。
"""
from __future__ import annotations

from ...reasons import (
    C_CLUSTER_CONFLICT,
    X_REF_CYCLE,
    X_REF_SELF,
    X_REF_UNKNOWN,
)

STATUS_DIRECT = "direct"
STATUS_RESOLVED = "resolved"
STATUS_HALTED = "halted"
STATUS_SETTLED = "settled"

STATUSES = (STATUS_DIRECT, STATUS_RESOLVED, STATUS_HALTED, STATUS_SETTLED)

STATUS_LABELS = {
    STATUS_DIRECT: "直付主档",
    STATUS_RESOLVED: "链式收敛",
    STATUS_HALTED: "挂起待核",
    STATUS_SETTLED: "人工落定",
}

DECIDE_ACTIONS = ("verify", "hold")


def edge_key(left: str, right: str) -> str:
    return f"{left}->{right}"


def _walk(index, supplier_id: str):
    """逐跳走完引用链，返回 (成功路径节点序列, 断点原因码, 断点边, 断点目标)。"""
    path = [supplier_id]
    visited = {supplier_id}
    current = supplier_id
    while True:
        target = index[current]["ref_master_id"]
        if not target:
            return path, "", "", ""
        key = edge_key(current, target)
        if target == current:
            return path, X_REF_SELF, key, target
        if target not in index:
            return path, X_REF_UNKNOWN, key, target
        if target in visited:
            return path, X_REF_CYCLE, key, target
        path.append(target)
        visited.add(target)
        current = target


def _chain_edges(path) -> list:
    return [{"from": path[i], "to": path[i + 1]} for i in range(len(path) - 1)]


def _fallback_payee(path, cluster_of, conflict):
    """最后已核节点：从路径末端回退，取第一个簇无冲突节点的簇代表元。"""
    for node in reversed(path):
        cluster = cluster_of.get(node)
        if cluster is not None and not conflict.get(cluster["cluster_id"], False):
            return cluster
    return None


def resolve_all(records, clusters, members, ref_decisions) -> list:
    """对整批记录逐条收敛，输出与记录一一对应的解析行。"""
    index = {record["supplier_id"]: record for record in records}
    conflict = {cluster["cluster_id"]: bool(cluster["conflict"]) for cluster in clusters}
    cluster_of = {}
    rep_of = {}
    for cluster in clusters:
        rep_of[cluster["cluster_id"]] = cluster["rep_id"]
    for member in members:
        cluster_of[member["supplier_id"]] = {
            "cluster_id": member["cluster_id"],
            "rep_id": rep_of.get(member["cluster_id"], ""),
        }

    rows = []
    for supplier_id in sorted(index):
        record = index[supplier_id]
        own_cluster = cluster_of.get(supplier_id)
        if not record["ref_master_id"]:
            # 无引用：直付自己簇代表元；簇冲突未决则挂起。
            if own_cluster is not None and conflict.get(own_cluster["cluster_id"], False):
                rows.append(_row(supplier_id, STATUS_HALTED, "", "", [],
                                 C_CLUSTER_CONFLICT, own_cluster["cluster_id"], ""))
            else:
                rows.append(_row(supplier_id, STATUS_DIRECT,
                                 own_cluster["rep_id"] if own_cluster else "",
                                 own_cluster["cluster_id"] if own_cluster else "",
                                 [], "", "", ""))
            continue

        path, reason, edge, target = _walk(index, supplier_id)
        chain = _chain_edges(path)
        if not reason:
            # 走到无引用末端：末端簇无冲突才收敛到最终主档。
            end_cluster = cluster_of.get(path[-1])
            if end_cluster is not None and not conflict.get(end_cluster["cluster_id"], False):
                rows.append(_row(supplier_id, STATUS_RESOLVED, end_cluster["rep_id"],
                                 end_cluster["cluster_id"], chain, "", "", ""))
                continue
            reason = C_CLUSTER_CONFLICT
            target = end_cluster["cluster_id"] if end_cluster else ""
            edge = ""

        decision = ref_decisions.get(edge) if edge else None
        verified = decision is not None and decision["decision"] == "verify"
        fallback = _fallback_payee(path, cluster_of, conflict)
        payee = fallback["rep_id"] if fallback else ""
        payee_cluster = fallback["cluster_id"] if fallback else ""
        detail = _detail(reason, path, target)
        if verified and fallback is not None:
            # 已核路径：人工确认停在最后已核节点，重跑不再改变。
            rows.append(_row(supplier_id, STATUS_SETTLED, payee, payee_cluster,
                             chain, reason, detail, edge))
        else:
            rows.append(_row(supplier_id, STATUS_HALTED, payee, payee_cluster,
                             chain, reason, detail, edge))
    return rows


def _detail(reason: str, path, target: str) -> str:
    if reason == C_CLUSTER_CONFLICT:
        return target
    if reason == X_REF_SELF:
        return target
    if reason == X_REF_CYCLE:
        return " -> ".join(path + [target])
    if reason == X_REF_UNKNOWN:
        return f"{path[-1]} -> {target}"
    return ""


def _row(supplier_id, status, payee_id, payee_cluster_id, chain, reason, detail, edge):
    return {
        "supplier_id": supplier_id,
        "status": status,
        "payee_id": payee_id,
        "payee_cluster_id": payee_cluster_id,
        "chain": chain,
        "reason_code": reason,
        "detail": detail,
        "edge_key": edge,
    }
