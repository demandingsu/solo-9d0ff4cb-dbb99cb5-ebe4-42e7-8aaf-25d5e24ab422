"""SQLite 持久化层。

并发与一致性策略：
- 文件库：WAL 模式，允许读写并发；写事务以 BEGIN IMMEDIATE 立即获取保留锁，
  多个写请求在锁上排队（busy_timeout 等待），写操作天然串行化。
- 内存库（IMAGE_LINEAGE_DB=:memory: 或 file:...?mode=memory 共享缓存 URI）：
  内存库随连接关闭即销毁，无法像文件库那样每请求新建连接；且共享缓存内存库
  使用表级锁、busy_timeout 不生效，并发读写会立即报 "database table is locked"。
  因此同一服务（进程）内复用单个长连接，以可重入锁串行化所有访问：
  写事务整体持锁保证整批原子，读快照事务整体持锁保证一次查询只见同一已提交状态。
- isolation_level=None（autocommit），所有事务边界显式管理。
- 文件库每个请求独立连接；读接口在单个 BEGIN..COMMIT 快照事务内完成全部查询，
  因此一次查询不会混用并发提交前后的谱系状态。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

# WAL 切换需要独占锁，多个连接/进程同时首启时必须串行，
# 否则 journal_mode=WAL 会抛 "database is locked"。
_init_lock = threading.Lock()
_wal_initialized: set[str] = set()

# 内存长连接 id → 事务串行锁（sqlite3.Connection 不支持动态属性/弱引用）
_memory_locks: dict[int, threading.RLock] = {}


def is_memory_db(db_path: str) -> bool:
    """是否为进程内临时内存库（重启即清空，不产生任何文件）。"""
    return db_path == ":memory:" or "?mode=memory" in db_path


class MemoryDatabase:
    """进程内临时内存库的连接生命周期管理。

    内存库的数据只活在持有它的连接里，而共享缓存内存库的表级锁又不支持
    busy_timeout 排队，因此这里维护一个长连接 + 一把可重入锁，供同一服务
    内的所有批次提交、修订历史与谱系查询共享已提交数据：

    - 不同服务（进程）的内存库天然互不可见，互不串库；
    - 服务重启（进程退出）后内存随之释放，临时数据清空；
    - 锁仅在事务区间（snapshot / 写事务）内由工作线程获取与释放：
      写事务整体持锁保证整批原子，读快照事务整体持锁保证一次查询只见
      同一已提交状态；锁不跨线程池工作线程持有，避免属主线程死锁。

    sqlite3.Connection 不支持动态属性与弱引用，故连接→锁的映射登记在
    模块级注册表，连接关闭时显式注销。
    """

    def __init__(self) -> None:
        # 私有匿名内存库：任何其他连接都无法打开它，库的生命与本连接一致
        self.conn = sqlite3.connect(
            ":memory:",
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        _memory_locks[id(self.conn)] = self.lock

    def close(self) -> None:
        with self.lock:
            _memory_locks.pop(id(self.conn), None)
            self.conn.close()


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT NOT NULL CHECK (status IN ('accepted', 'duplicate', 'rejected')),
    item_count      INTEGER NOT NULL,
    inserted_count  INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    submitted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    product_id         TEXT NOT NULL,
    revision           INTEGER NOT NULL,
    summary            TEXT NOT NULL,
    parent_product_id  TEXT,
    parent_revision    INTEGER,
    batch_id           INTEGER NOT NULL REFERENCES batches(batch_id),
    submitted_at       TEXT NOT NULL,
    PRIMARY KEY (product_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_revisions_parent
    ON revisions(parent_product_id, parent_revision);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个带推荐 PRAGMA 的连接（每次请求新建、用完关闭）。"""
    conn = sqlite3.connect(
        db_path,
        isolation_level=None,  # autocommit：事务边界完全由代码控制
        timeout=30.0,          # 写锁排队最多等待 30s
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_wal(conn, db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_wal(conn: sqlite3.Connection, db_path: str) -> None:
    """线程安全地把文件库切换到 WAL（每个进程每个库只切一次）。"""
    if is_memory_db(db_path):
        return  # 内存库无需 WAL
    with _init_lock:
        if db_path in _wal_initialized:
            return
        # busy_timeout 已设置；若另一进程正在切换，这里会等待而非立即报错
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_initialized.add(db_path)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


@contextmanager
def _txn_guard(conn: sqlite3.Connection) -> Iterator[None]:
    """内存连接在事务区间持可重入锁串行化；文件连接无需额外锁（WAL 负责并发）。"""
    lock = _memory_locks.get(id(conn))
    if lock is None:
        yield
    else:
        with lock:
            yield


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """只读快照事务：事务内的所有 SELECT 看到同一个已提交快照。

    内存库下整个事务区间持锁，并发提交只能在快照结束后进入，
    因此一次查询绝不会混用两次提交前后的谱系状态。
    """
    with _txn_guard(conn):
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# 读取辅助（须在已开启的事务/快照内调用）
# ---------------------------------------------------------------------------

def get_revision(
    conn: sqlite3.Connection, product_id: str, revision: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM revisions WHERE product_id = ? AND revision = ?",
        (product_id, revision),
    ).fetchone()


def get_active_revision(
    conn: sqlite3.Connection, product_id: str
) -> Optional[sqlite3.Row]:
    """返回某成果号下最高已提交修订对应的整行；无成果则 None。"""
    return conn.execute(
        "SELECT * FROM revisions WHERE product_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (product_id,),
    ).fetchone()


def list_active_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """所有成果号各自的现役（最高修订）行。"""
    return list(
        conn.execute(
            "SELECT r.* FROM revisions r "
            "JOIN (SELECT product_id, MAX(revision) AS max_rev "
            "      FROM revisions GROUP BY product_id) m "
            "  ON r.product_id = m.product_id AND r.revision = m.max_rev "
            "ORDER BY r.product_id"
        )
    )


def list_revisions(conn: sqlite3.Connection, product_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM revisions WHERE product_id = ? ORDER BY revision",
            (product_id,),
        )
    )


def get_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM batches ORDER BY batch_id"))


# ---------------------------------------------------------------------------
# 写入：整批提交
# ---------------------------------------------------------------------------

def _insert_batch_record(
    conn: sqlite3.Connection,
    status: str,
    item_count: int,
    inserted_count: int,
    note: Optional[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO batches (status, item_count, inserted_count, note, submitted_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (status, item_count, inserted_count, note, utc_now_iso()),
    )
    return int(cur.lastrowid)


def submit_batch(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> dict[str, Any]:
    """原子提交一批清单。

    返回 dict：status ∈ accepted | duplicate | rejected，
    rejected / duplicate 时 revisions 表不产生任何写入（rejected 仅留批次审计记录）。
    """
    item_count = len(items)

    # 1) 批内去重：同号同修订出现多次时，内容必须完全一致，否则整批拒收
    deduped: dict[tuple[str, int], dict[str, Any]] = {}
    for item in items:
        key = (item["product_id"], item["revision"])
        if key in deduped:
            if _content_differs(deduped[key], item):
                batch_id = _record_rejected(
                    conn, item_count,
                    f"批内存在同号同修订但清单不同的条目: {key[0]}@{key[1]}",
                )
                return {
                    "batch_id": batch_id,
                    "status": "rejected",
                    "item_count": item_count,
                    "inserted_count": 0,
                    "conflicts": [{
                        "product_id": key[0],
                        "revision": key[1],
                        "submitted": item,
                        "existing": deduped[key],
                    }],
                    "message": "整批拒收：批内同号同修订条目内容不一致，未写入任何数据",
                }
            # 完全相同的重复条目：折叠为一条
        else:
            deduped[key] = item

    # 2) 与库内已提交数据核对（在写事务内，锁定后复查，防止并发写交叉）
    with _txn_guard(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conflicts: list[dict[str, Any]] = []
            identical_existing: set[tuple[str, int]] = set()
            for key, item in deduped.items():
                row = get_revision(conn, key[0], key[1])
                if row is None:
                    continue
                existing = _row_to_item(row)
                if _content_differs(existing, item):
                    conflicts.append({
                        "product_id": key[0],
                        "revision": key[1],
                        "submitted": item,
                        "existing": existing,
                    })
                else:
                    identical_existing.add(key)

            if conflicts:
                # 同号同修订但清单不同 → 整批拒收，revisions 零写入
                batch_id = _insert_batch_record(
                    conn, "rejected", item_count, 0,
                    "同号同修订但清单不同，整批拒收",
                )
                conn.execute("COMMIT")
                return {
                    "batch_id": batch_id,
                    "status": "rejected",
                    "item_count": item_count,
                    "inserted_count": 0,
                    "conflicts": conflicts,
                    "message": "整批拒收：存在同号同修订但清单不同的条目，原有结论保持不变",
                }

            now = utc_now_iso()
            if len(identical_existing) == len(deduped):
                # 整批为完全重复提交：幂等处理，不重复写 revisions
                batch_id = _insert_batch_record(
                    conn, "duplicate", item_count, 0,
                    "与已提交清单完全一致，幂等接收且未重复写入",
                )
                conn.execute("COMMIT")
                return {
                    "batch_id": batch_id,
                    "status": "duplicate",
                    "item_count": item_count,
                    "inserted_count": 0,
                    "conflicts": [],
                    "message": "清单与已提交内容完全一致，按重复提交处理（幂等）",
                }

            # 3) 写入新条目 + accepted 批次记录，同一事务原子提交
            batch_id = _insert_batch_record(
                conn, "accepted", item_count,
                len(deduped) - len(identical_existing), None,
            )
            for key, item in deduped.items():
                if key in identical_existing:
                    continue
                conn.execute(
                    "INSERT INTO revisions (product_id, revision, summary, "
                    "parent_product_id, parent_revision, batch_id, submitted_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["product_id"], item["revision"], item["summary"],
                        item["parent_product_id"], item["parent_revision"],
                        batch_id, now,
                    ),
                )
            conn.execute("COMMIT")
            return {
                "batch_id": batch_id,
                "status": "accepted",
                "item_count": item_count,
                "inserted_count": len(deduped) - len(identical_existing),
                "conflicts": [],
                "message": "批次已提交",
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _record_rejected(
    conn: sqlite3.Connection, item_count: int, note: str
) -> int:
    """在独立短事务中记录拒收批次（不触碰 revisions，原结论不变）。"""
    with _txn_guard(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            batch_id = _insert_batch_record(conn, "rejected", item_count, 0, note)
            conn.execute("COMMIT")
            return batch_id
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _content_differs(a: dict[str, Any], b: dict[str, Any]) -> bool:
    fields = ("summary", "parent_product_id", "parent_revision")
    return any(a[f] != b[f] for f in fields)


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "product_id": row["product_id"],
        "revision": row["revision"],
        "summary": row["summary"],
        "parent_product_id": row["parent_product_id"],
        "parent_revision": row["parent_revision"],
    }
