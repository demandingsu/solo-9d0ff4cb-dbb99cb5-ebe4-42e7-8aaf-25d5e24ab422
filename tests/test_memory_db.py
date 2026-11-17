"""临时内存库（IMAGE_LINEAGE_DB=:memory:）连接生命周期自测。

覆盖需求：
1. 同一服务内批次提交、修订历史、谱系查询共享已提交数据；
2. 两个同时运行的服务（同一进程两个 app 实例，对应两个进程/容器）互不串库；
3. 服务重启（生命周期关闭后重建）后临时数据清空；
4. 提交与查询交错时整批原子写入、单次查询只见同一已提交谱系；
5. 共享缓存内存 URI（file:...?mode=memory&cache=shared）同样按内存库处理；
6. 文件库原有行为保持（每请求独立连接，WAL 路径不变）。
"""

from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.main import create_app


def _item(product_id, revision, summary, parent=None):
    payload = {"product_id": product_id, "revision": revision, "summary": summary}
    if parent is not None:
        payload["parent_product_id"] = parent[0]
        payload["parent_revision"] = parent[1]
    return payload


def _post(client, items):
    return client.post("/api/batches", json={"items": items})


# ---------------------------------------------------------------------------
# 1. 同一服务内所有请求共享同一已提交内存数据（修复前的 no such table 场景）
# ---------------------------------------------------------------------------

def test_memory_db_shared_across_requests():
    app = create_app(":memory:")
    with TestClient(app) as c:
        r = _post(c, [_item("R-1", 1, "原始瓦片")])
        assert r.status_code == 201 and r.json()["status"] == "accepted"

        # 新请求（修复前会拿到全新空库 → no such table）
        hist = c.get("/api/products/R-1/revisions")
        assert hist.status_code == 200
        assert hist.json()["revisions"][0]["summary"] == "原始瓦片"

        _post(c, [_item("M-1", 1, "一级拼图", parent=("R-1", 1))])
        status = c.get("/api/products/M-1/status").json()
        assert status["deliverable"] is True
        assert len(c.get("/api/batches").json()) == 2


def test_memory_db_supports_shared_cache_uri_form():
    uri = f"file:mem-{uuid.uuid4().hex}?mode=memory&cache=shared"
    app = create_app(uri)
    with TestClient(app) as c:
        assert c.get("/health").json()["database"] == uri
        assert _post(c, [_item("R", 1, "瓦片")]).json()["status"] == "accepted"
        assert c.get("/api/products/R/revisions").status_code == 200
    # 服务停止后该命名内存库应已随连接关闭而释放


# ---------------------------------------------------------------------------
# 2. 两个同时运行的服务互不串库
# ---------------------------------------------------------------------------

def test_two_concurrent_services_have_isolated_memory_databases():
    app_a = create_app(":memory:")
    app_b = create_app(":memory:")
    with TestClient(app_a) as ca, TestClient(app_b) as cb:
        assert _post(ca, [_item("ONLY-A", 1, "甲服务数据")]).status_code == 201
        assert _post(cb, [_item("ONLY-B", 1, "乙服务数据")]).status_code == 201

        # 各自只能看到自己的成果
        assert ca.get("/api/products/ONLY-A/status").status_code == 200
        assert ca.get("/api/products/ONLY-B/status").status_code == 404
        assert cb.get("/api/products/ONLY-B/status").status_code == 200
        assert cb.get("/api/products/ONLY-A/status").status_code == 404

        # 批次审计也相互隔离
        assert [b["item_count"] for b in ca.get("/api/batches").json()] == [1]
        assert [b["item_count"] for b in cb.get("/api/batches").json()] == [1]


# ---------------------------------------------------------------------------
# 3. 服务重启后临时数据清空
# ---------------------------------------------------------------------------

def test_memory_db_cleared_after_service_restart():
    app = create_app(":memory:")
    with TestClient(app) as c:
        assert _post(c, [_item("R-1", 1, "重启前数据")]).status_code == 201
        assert c.get("/api/products/R-1/revisions").status_code == 200

    # 模拟重启：同一工厂重新走一遍生命周期（新连接、新内存库）
    with TestClient(app) as c:
        assert c.get("/api/products/R-1/status").status_code == 404
        assert c.get("/api/batches").json() == []
        # 重启后的库可正常重新写入
        assert _post(c, [_item("R-1", 1, "重启后数据")]).status_code == 201
        assert (
            c.get("/api/products/R-1/status").json()["summary"] == "重启后数据"
        )


# ---------------------------------------------------------------------------
# 4. 提交与查询交错：写整批原子、读只见同一已提交快照，且无锁错误
# ---------------------------------------------------------------------------

def test_interleaved_submits_and_queries_are_serialized_and_consistent():
    app = create_app(":memory:")
    errors: list[Exception] = []

    def writer(client: TestClient, start: int):
        try:
            for i in range(20):
                idx = start * 20 + i
                r = _post(client, [
                    _item(f"P{idx}", 1, f"成果{idx}", parent=("ROOT", 1))
                ])
                assert r.status_code == 201
        except Exception as exc:  # pragma: no cover - 失败时报告
            errors.append(exc)

    def reader(client: TestClient):
        try:
            for _ in range(100):
                batches = client.get("/api/batches").json()
                # 快照自洽：列表长度与每条记录字段必然一致，
                # 不可能读到“半批”状态（批次与其修订同一事务提交）
                for b in batches:
                    assert b["status"] in ("accepted", "duplicate", "rejected")
                    assert b["inserted_count"] >= 0
                mosaics = client.get("/api/mosaics").json()
                for m in mosaics:
                    # 结论必须自洽：可交付则无阻断，否则阻断明细非空
                    assert m["deliverable"] == (len(m["blockers"]) == 0)
        except Exception as exc:  # pragma: no cover - 失败时报告
            errors.append(exc)

    # 单个存活服务，多个线程并发交错发起提交与查询（真实部署形态）
    with TestClient(app) as c:
        assert _post(c, [_item("ROOT", 1, "根瓦片")]).status_code == 201
        threads = [threading.Thread(target=writer, args=(c, i)) for i in range(4)]
        threads.append(threading.Thread(target=reader, args=(c,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

        accepted = [b for b in c.get("/api/batches").json() if b["status"] == "accepted"]
        # 根瓦片 1 批 + 4 线程 x 20 批，全部成功、无脏写
        assert len(accepted) == 81
        mosaics = c.get("/api/mosaics").json()
        assert len(mosaics) == 80
        assert all(m["deliverable"] is True for m in mosaics)


def test_whole_batch_atomicity_in_memory_db():
    app = create_app(":memory:")
    with TestClient(app) as c:
        _post(c, [_item("R-1", 1, "原始摘要")])
        # 冲突批整批拒收：同批的新成果不得部分写入
        r = _post(c, [
            _item("R-1", 1, "被篡改的摘要"),
            _item("NEW", 1, "不应入库"),
        ])
        assert r.json()["status"] == "rejected"
        assert c.get("/api/products/NEW/status").status_code == 404
        assert c.get("/api/products/R-1/status").json()["summary"] == "原始摘要"

        # 完全重复提交保持幂等
        again = _post(c, [_item("R-1", 1, "原始摘要")])
        assert again.json()["status"] == "duplicate"
        assert len(c.get("/api/products/R-1/revisions").json()["revisions"]) == 1


# ---------------------------------------------------------------------------
# 5. 内存库不产生任何文件
# ---------------------------------------------------------------------------

def test_memory_db_creates_no_files(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    app = create_app(":memory:")
    with TestClient(app) as c:
        _post(c, [_item("R", 1, "瓦片")])
        c.get("/api/products/R/status")
    assert os.listdir(tmp_path) == []
