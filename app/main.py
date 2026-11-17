"""FastAPI 应用：影像谱系 API。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
文档：启动后访问 /docs（Swagger UI，可直接发起请求）。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from . import database as db
from . import lineage
from .schemas import (
    BatchRecord,
    BatchResponse,
    BatchSubmission,
    HealthResponse,
    ProductStatus,
    RevisionHistoryItem,
    RevisionHistoryResponse,
)

DEFAULT_DB_PATH = os.environ.get("IMAGE_LINEAGE_DB", "data/lineage.db")


def get_db_path(request: Request) -> str:
    return request.app.state.db_path


def get_conn(request: Request) -> sqlite3.Connection:
    """每请求一个独立连接，响应结束即关闭。"""
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def create_app(db_path: Optional[str] = None) -> FastAPI:
    resolved_path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db_path = resolved_path
        # 确保文件目录存在并初始化表结构
        if resolved_path != ":memory:" and "?mode=memory" not in resolved_path:
            parent = os.path.dirname(os.path.abspath(resolved_path))
            os.makedirs(parent, exist_ok=True)
        init_conn = db.connect(resolved_path)
        try:
            db.init_db(init_conn)
        finally:
            init_conn.close()
        yield

    app = FastAPI(
        title="影像谱系 API",
        description=(
            "分批提交原始瓦片与拼图清单，追踪多级固定父引用，"
            "查询现役拼图的可交付状态及受阻引用路径（父项缺失 / 父项已非现役 / 循环引用）。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    # ---------------- 健康检查 ----------------
    @app.get("/health", response_model=HealthResponse, tags=["系统"])
    def health(request: Request) -> HealthResponse:
        return HealthResponse(status="ok", database=request.app.state.db_path)

    # ---------------- 分批提交 ----------------
    @app.post(
        "/api/batches",
        response_model=BatchResponse,
        status_code=201,
        tags=["清单提交"],
        summary="分批提交原始瓦片/拼图的 JSON 清单",
    )
    def submit(
        payload: BatchSubmission,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> BatchResponse:
        items = [item.model_dump() for item in payload.items]
        result = db.submit_batch(conn, items)
        return BatchResponse(**result)

    @app.get("/api/batches", response_model=list[BatchRecord], tags=["清单提交"])
    def list_batches(conn: sqlite3.Connection = Depends(get_conn)) -> list[BatchRecord]:
        with db.snapshot(conn):
            rows = db.list_batches(conn)
        return [
            BatchRecord(
                batch_id=r["batch_id"],
                status=r["status"],
                item_count=r["item_count"],
                inserted_count=r["inserted_count"],
                note=r["note"],
                submitted_at=r["submitted_at"],
            )
            for r in rows
        ]

    @app.get("/api/batches/{batch_id}", response_model=BatchRecord, tags=["清单提交"])
    def get_batch(
        batch_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> BatchRecord:
        with db.snapshot(conn):
            row = db.get_batch(conn, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
        return BatchRecord(
            batch_id=row["batch_id"],
            status=row["status"],
            item_count=row["item_count"],
            inserted_count=row["inserted_count"],
            note=row["note"],
            submitted_at=row["submitted_at"],
        )

    # ---------------- 可交付状态 ----------------
    @app.get(
        "/api/products/{product_id}/status",
        response_model=ProductStatus,
        tags=["状态查询"],
        summary="查询成果（默认现役修订）的可交付状态与受阻引用路径",
    )
    def product_status(
        product_id: str,
        revision: Optional[int] = Query(
            default=None, ge=1, description="指定修订号；省略时查询现役（最高已提交）修订"
        ),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> ProductStatus:
        # 整个评估在同一个快照事务内完成，不混用并发提交前后的谱系
        with db.snapshot(conn):
            result = lineage.evaluate_product(conn, product_id, revision)
        if result is None:
            if revision is None:
                raise HTTPException(
                    status_code=404, detail=f"成果 {product_id} 尚未提交任何修订"
                )
            raise HTTPException(
                status_code=404,
                detail=f"成果 {product_id} 的修订 {revision} 不存在",
            )
        return ProductStatus(**result)

    @app.get(
        "/api/mosaics",
        response_model=list[ProductStatus],
        tags=["状态查询"],
        summary="列出所有现役拼图及其可交付状态，可按结论过滤",
    )
    def list_mosaics(
        deliverable: Optional[bool] = Query(
            default=None, description="true=仅可交付；false=仅受阻；省略=全部"
        ),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> list[ProductStatus]:
        with db.snapshot(conn):
            active_rows = db.list_active_products(conn)
            results = []
            for row in active_rows:
                if row["parent_product_id"] is None:
                    continue  # 只列拼图（有父引用的成果）
                status = lineage.evaluate(conn, row, start_is_active=True)
                if deliverable is None or status["deliverable"] is deliverable:
                    results.append(ProductStatus(**status))
        return results

    # ---------------- 修订追溯 ----------------
    @app.get(
        "/api/products/{product_id}/revisions",
        response_model=RevisionHistoryResponse,
        tags=["修订追溯"],
        summary="列出某成果号的全部已提交修订，标注现役修订（旧修订保留可追溯）",
    )
    def revision_history(
        product_id: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> RevisionHistoryResponse:
        with db.snapshot(conn):
            rows = db.list_revisions(conn, product_id)
            if not rows:
                raise HTTPException(
                    status_code=404, detail=f"成果 {product_id} 尚未提交任何修订"
                )
            active = db.get_active_revision(conn, product_id)
            active_rev = active["revision"] if active else None
            items = [
                RevisionHistoryItem(
                    revision=r["revision"],
                    summary=r["summary"],
                    parent_product_id=r["parent_product_id"],
                    parent_revision=r["parent_revision"],
                    is_active=(r["revision"] == active_rev),
                    batch_id=r["batch_id"],
                    submitted_at=r["submitted_at"],
                )
                for r in rows
            ]
        return RevisionHistoryResponse(
            product_id=product_id, active_revision=active_rev, revisions=items
        )

    return app


app = create_app()
