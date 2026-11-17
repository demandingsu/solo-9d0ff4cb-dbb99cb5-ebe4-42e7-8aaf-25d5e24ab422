"""端到端自测：覆盖需求中的全部关键情形。

运行：pytest -q（需在项目根目录，且依赖已安装）
"""

from __future__ import annotations

import os
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def _item(product_id, revision, summary, parent=None):
    payload = {"product_id": product_id, "revision": revision, "summary": summary}
    if parent is not None:
        payload["parent_product_id"] = parent[0]
        payload["parent_revision"] = parent[1]
    return payload


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    app = create_app(db_path)
    with TestClient(app) as c:
        yield c


def _post(client, items):
    return client.post("/api/batches", json={"items": items})


# ---------------------------------------------------------------------------
# 1. 基础校验
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_parent_pair_must_be_complete(client):
    # 只给父成果号、不给父修订号 → 422
    bad = [_item("M-1", 1, "缺父修订")]
    bad[0]["parent_product_id"] = "R-1"
    r = _post(client, bad)
    assert r.status_code == 422


def test_empty_batch_rejected_by_validation(client):
    r = client.post("/api/batches", json={"items": []})
    assert r.status_code == 422


def test_tile_is_always_deliverable(client):
    r = _post(client, [_item("R-1", 1, "原始瓦片")])
    assert r.status_code == 201 and r.json()["status"] == "accepted"
    s = client.get("/api/products/R-1/status").json()
    assert s["kind"] == "tile" and s["deliverable"] is True and s["blockers"] == []


# ---------------------------------------------------------------------------
# 2. 父项缺失与父项晚到
# ---------------------------------------------------------------------------

def test_missing_parent_blocks_then_late_parent_unblocks(client):
    # 拼图先到，引用尚未提交的瓦片
    r = _post(client, [_item("M-1", 1, "一级拼图", parent=("R-9", 1))])
    assert r.json()["status"] == "accepted"

    s = client.get("/api/products/M-1/status").json()
    assert s["deliverable"] is False
    assert len(s["blockers"]) == 1
    b = s["blockers"][0]
    assert b["reason"] == "missing"
    assert b["path"] == ["M-1@1", "R-9@1"]

    # 父项晚到：补传后结论随已提交状态自动变化
    r = _post(client, [_item("R-9", 1, "晚到的瓦片")])
    assert r.json()["status"] == "accepted"
    s = client.get("/api/products/M-1/status").json()
    assert s["deliverable"] is True and s["blockers"] == []


# ---------------------------------------------------------------------------
# 3. 父项已非现役（上游更新）
# ---------------------------------------------------------------------------

def test_superseded_parent_blocks_and_new_revision_resolves(client):
    _post(client, [_item("R-1", 1, "瓦片v1")])
    _post(client, [_item("M-1", 1, "拼图固定引用v1", parent=("R-1", 1))])
    assert client.get("/api/products/M-1/status").json()["deliverable"] is True

    # 上游提交更高修订 → R-1@1 退役，M-1@1 受阻
    _post(client, [_item("R-1", 2, "瓦片v2")])
    s = client.get("/api/products/M-1/status").json()
    assert s["deliverable"] is False
    b = s["blockers"][0]
    assert b["reason"] == "superseded"
    assert b["path"] == ["M-1@1", "R-1@1"]
    assert "R-1@2" in b["detail"]

    # 拼图发布新修订，改引现役父项 → 恢复可交付；旧修订仍可追溯且仍标记受阻
    _post(client, [_item("M-1", 2, "拼图改引v2", parent=("R-1", 2))])
    active = client.get("/api/products/M-1/status").json()
    assert active["revision"] == 2 and active["deliverable"] is True

    old = client.get("/api/products/M-1/status", params={"revision": 1}).json()
    assert old["is_active"] is False and old["deliverable"] is False

    hist = client.get("/api/products/M-1/revisions").json()
    assert hist["active_revision"] == 2
    assert [r["revision"] for r in hist["revisions"]] == [1, 2]


# ---------------------------------------------------------------------------
# 4. 循环引用
# ---------------------------------------------------------------------------

def test_cycle_detection(client):
    # A@1 -> B@1 -> A@1，闭环
    _post(client, [
        _item("A", 1, "拼图A", parent=("B", 1)),
        _item("B", 1, "拼图B", parent=("A", 1)),
    ])
    for pid in ("A", "B"):
        s = client.get(f"/api/products/{pid}/status").json()
        assert s["deliverable"] is False
        assert s["blockers"][0]["reason"] == "cycle"
    # 路径应展示到回环节点
    s = client.get("/api/products/A/status").json()
    assert s["blockers"][0]["path"] == ["A@1", "B@1", "A@1"]


def test_self_reference_is_cycle(client):
    _post(client, [_item("S", 1, "自引用", parent=("S", 1))])
    s = client.get("/api/products/S/status").json()
    assert s["deliverable"] is False and s["blockers"][0]["reason"] == "cycle"


# ---------------------------------------------------------------------------
# 5. 多级链路与多阻断收集
# ---------------------------------------------------------------------------

def test_multi_level_chain(client):
    _post(client, [
        _item("R", 1, "瓦片"),
        _item("M1", 1, "一级拼图", parent=("R", 1)),
        _item("M2", 1, "二级拼图", parent=("M1", 1)),
        _item("M3", 1, "三级拼图", parent=("M2", 1)),
    ])
    s = client.get("/api/products/M3/status").json()
    assert s["deliverable"] is True and s["blockers"] == []

    # 最底层瓦片升级 → 整条链全部受阻，路径逐级指向问题节点
    _post(client, [_item("R", 2, "瓦片v2")])
    s = client.get("/api/products/M3/status").json()
    assert s["deliverable"] is False
    assert s["blockers"][0]["path"] == ["M3@1", "M2@1", "M1@1", "R@1"]


def test_deep_missing_parent_path(client):
    _post(client, [
        _item("M2", 1, "二级", parent=("M1", 1)),
        _item("M1", 1, "一级", parent=("R", 1)),
    ])
    s = client.get("/api/products/M2/status").json()
    assert s["deliverable"] is False
    assert s["blockers"][0]["reason"] == "missing"
    assert s["blockers"][0]["path"] == ["M2@1", "M1@1", "R@1"]


# ---------------------------------------------------------------------------
# 6. 整批拒收与幂等
# ---------------------------------------------------------------------------

def test_conflicting_manifest_rejects_whole_batch_and_keeps_old_conclusion(client):
    _post(client, [
        _item("R-1", 1, "原始摘要"),
        _item("M-1", 1, "拼图", parent=("R-1", 1)),
    ])
    assert client.get("/api/products/M-1/status").json()["deliverable"] is True

    # 同号同修订但摘要不同 → 整批拒收（即便同批还有无关新成果也不写入）
    r = _post(client, [
        _item("R-1", 1, "被篡改的摘要"),
        _item("NEW", 1, "本不应入库的新成果"),
    ])
    body = r.json()
    assert r.status_code == 201
    assert body["status"] == "rejected"
    assert body["inserted_count"] == 0
    assert body["conflicts"][0]["product_id"] == "R-1"

    # 原结论不变；无关新成果确实未入库
    assert client.get("/api/products/R-1/status").json()["summary"] == "原始摘要"
    assert client.get("/api/products/NEW/status").status_code == 404
    assert client.get("/api/products/M-1/status").json()["deliverable"] is True

    # 拒收批次留审计记录
    batches = client.get("/api/batches").json()
    assert batches[-1]["status"] == "rejected"


def test_identical_resubmission_is_idempotent(client):
    payload = [_item("R-1", 1, "摘要", parent=None)]
    first = _post(client, payload).json()
    second = _post(client, payload).json()
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate" and second["inserted_count"] == 0

    # 批次号不同（留痕），但修订仍只有一条
    assert first["batch_id"] != second["batch_id"]
    hist = client.get("/api/products/R-1/revisions").json()
    assert len(hist["revisions"]) == 1


def test_intra_batch_duplicate_inconsistent_rejected(client):
    r = _post(client, [
        _item("R-1", 1, "摘要A"),
        _item("R-1", 1, "摘要B"),
    ])
    assert r.json()["status"] == "rejected"
    assert client.get("/api/products/R-1/status").status_code == 404


def test_intra_batch_duplicate_identical_dedup(client):
    item = _item("R-1", 1, "摘要A")
    r = _post(client, [dict(item), dict(item)])
    body = r.json()
    assert body["status"] == "accepted" and body["item_count"] == 2
    assert body["inserted_count"] == 1


# ---------------------------------------------------------------------------
# 7. 列表与过滤
# ---------------------------------------------------------------------------

def test_mosaic_listing_and_filter(client):
    _post(client, [
        _item("R", 1, "瓦片"),
        _item("OK", 1, "可交付拼图", parent=("R", 1)),
        _item("BAD", 1, "受阻拼图", parent=("GHOST", 1)),
    ])
    all_mosaics = client.get("/api/mosaics").json()
    ids = {m["product_id"] for m in all_mosaics}
    assert ids == {"OK", "BAD"}  # 瓦片 R 不出现在拼图列表

    blocked = client.get("/api/mosaics", params={"deliverable": False}).json()
    assert [m["product_id"] for m in blocked] == ["BAD"]
    ready = client.get("/api/mosaics", params={"deliverable": True}).json()
    assert [m["product_id"] for m in ready] == ["OK"]


# ---------------------------------------------------------------------------
# 8. 并发：查询不混用提交前后谱系；写事务原子且串行
# ---------------------------------------------------------------------------

def test_concurrent_writers_are_serialized_and_atomic(client):
    base = f"{tempfile.gettempdir()}/lineage-conc.db"
    if os.path.exists(base):
        os.remove(base)
    app = create_app(base)
    errors: list[Exception] = []

    def worker(start: int):
        try:
            with TestClient(app) as c:
                items = [
                    _item(f"P{start}", 1, f"成果{start}", parent=(f"P{start-1}", 1))
                ]
                r = c.post("/api/batches", json={"items": items})
                assert r.status_code == 201
        except Exception as exc:  # pragma: no cover - 失败时报告
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    with TestClient(app) as c:
        batches = c.get("/api/batches").json()
        accepted = [b for b in batches if b["status"] == "accepted"]
        assert len(accepted) == 8
        # 每个成果恰好一条修订，无脏写/交叉
        for i in range(8):
            hist = c.get(f"/api/products/P{i}/revisions").json()
            assert len(hist["revisions"]) == 1
    for suffix in ("", "-wal", "-shm"):
        p = base + suffix
        if os.path.exists(p):
            os.remove(p)


def test_snapshot_does_not_mix_states(client):
    # 初始：可交付的两级拼图
    _post(client, [
        _item("R", 1, "瓦片"),
        _item("M", 1, "拼图", parent=("R", 1)),
    ])

    # 并发更新线程在查询进行中提交上游新修订；
    # 单次 /status 内的所有读取处于同一快照，结论必须自洽。
    def updater():
        _post(client, [_item("R", 2, "瓦片v2")])

    results = []
    t = threading.Thread(target=updater)
    t.start()
    for _ in range(5):
        s = client.get("/api/products/M/status").json()
        # 无论拿到旧快照还是新快照：deliverable 与 blockers 必须一致自洽
        if s["deliverable"]:
            assert s["blockers"] == []
        else:
            assert s["blockers"][0]["reason"] == "superseded"
        results.append((s["deliverable"], len(s["blockers"])))
    t.join()
    assert results  # 确认确实执行了查询
    # 更新结束后的最终结论稳定为受阻
    final = client.get("/api/products/M/status").json()
    assert final["deliverable"] is False


def test_concurrent_conflicting_writes_serialize():
    # 两个线程同时提交同号同修订但内容不同的清单：
    # 一个 accepted，另一个必须整批 rejected，无脏数据，且不发生锁错误。
    dbp = os.path.join(tempfile.gettempdir(), "lineage-conflict-pytest.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(dbp + suffix):
            os.remove(dbp + suffix)
    app = create_app(dbp)
    outcomes: list[tuple[str, str, int]] = []

    def worker(summary: str):
        with TestClient(app) as c:
            r = c.post("/api/batches", json={"items": [
                _item("X", 1, summary)
            ]})
            body = r.json()
            outcomes.append((summary, body["status"], body["inserted_count"]))

    t1 = threading.Thread(target=worker, args=("摘要A",))
    t2 = threading.Thread(target=worker, args=("摘要B-不同",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(o[1] for o in outcomes) == ["accepted", "rejected"]
    with TestClient(app) as c:
        surviving = c.get("/api/products/X/status").json()
        assert surviving["summary"] == "摘要A"
        statuses = [b["status"] for b in c.get("/api/batches").json()]
        assert sorted(statuses) == ["accepted", "rejected"]
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(dbp + suffix):
            os.remove(dbp + suffix)


def test_404_cases(client):
    assert client.get("/api/products/UNKNOWN/status").status_code == 404
    assert client.get(
        "/api/products/UNKNOWN/status", params={"revision": 3}
    ).status_code == 404
    assert client.get("/api/batches/999").status_code == 404
