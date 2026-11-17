"""付款清单 CSV：与页面读取同一生效代，收款主体与链路证据一致。"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from ...reasons import label_of
from . import chain

HEADER = ("供应商号", "名称", "声明主档", "收敛状态", "收款主体", "收款主体名称",
          "收款主体税号", "收款主体编号", "链路证据", "未决原因码", "未决原因",
          "断点详情", "本年累计(元)")


def _evidence(item) -> str:
    hops = [item["supplier_id"]]
    for edge in item["chain"]:
        hops.append(edge["to"])
    return " -> ".join(hops) if len(hops) > 1 else ""


def render(items) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for item in items:
        writer.writerow([
            item["supplier_id"],
            item["raw_name"],
            item["ref_master_id"],
            chain.STATUS_LABELS[item["status"]],
            item["payee_id"],
            item["payee_name"],
            item["payee_tax"],
            item["payee_cluster_id"],
            _evidence(item),
            item["reason_code"],
            label_of(item["reason_code"]) if item["reason_code"] else "",
            item["detail"],
            f"{Decimal(item['ytd_amount']):.2f}",
        ])
    return buffer.getvalue()
