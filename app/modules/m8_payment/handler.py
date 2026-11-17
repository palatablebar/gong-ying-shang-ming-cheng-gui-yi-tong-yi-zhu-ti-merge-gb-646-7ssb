"""M8 路由：付款清单、收敛重算、断点边决定、付款 CSV 导出。"""
from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from ..m1_intake import service as intake_service
from . import payment_out, service

router = APIRouter(prefix="/api", tags=["m8-payment"])

CSV_TYPE = "text/csv; charset=utf-8"


class RefDecisionRequest(BaseModel):
    decision: str = Field(min_length=1, max_length=10)
    expected_version: int = Field(ge=0)
    operator: str = Field(default="采购主数据组", max_length=40)


@router.get("/payment")
def payment_list(limit: int = 200):
    run = intake_service.ensure_run()
    items = service.payment_list(run["run_id"])
    return {"run_id": run["run_id"], "stats": service.summary(run["run_id"]),
            "total": len(items), "items": items[:limit]}


@router.get("/payment/stats")
def payment_stats():
    run = intake_service.ensure_run()
    service.ensure_resolutions(run["run_id"])
    return service.summary(run["run_id"])


@router.post("/payment/resolve")
def payment_resolve():
    run = intake_service.ensure_run()
    return service.ensure_resolutions(run["run_id"])


@router.post("/payment/{supplier_id}/decision")
def payment_decide(supplier_id: str, body: RefDecisionRequest):
    run = intake_service.ensure_run()
    record = service.decide(run["run_id"], supplier_id, body.decision,
                            body.expected_version, body.operator)
    return {"run_id": run["run_id"], "decision": record,
            "stats": service.summary(run["run_id"])}


@router.get("/export/payment.csv")
def export_payment():
    run = intake_service.ensure_run()
    content = payment_out.render(service.payment_list(run["run_id"]))
    return Response(
        content="\ufeff" + content,
        media_type=CSV_TYPE,
        headers={"Content-Disposition":
                 f'attachment; filename="payment-{run["run_id"]}.csv"'},
    )
