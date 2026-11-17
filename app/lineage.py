"""谱系评估：沿固定父引用链上行，判定可交付状态并给出阻断路径。"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from . import database as db
from .schemas import (
    BLOCKER_LABELS,
    REASON_CYCLE,
    REASON_MISSING,
    REASON_SUPERSEDED,
)


def _node_label(product_id: str, revision: int) -> str:
    return f"{product_id}@{revision}"


def evaluate(
    conn: sqlite3.Connection,
    start_row: sqlite3.Row,
    *,
    start_is_active: bool,
) -> dict[str, Any]:
    """评估某个已存在修订行的整条谱系。

    调用方须保证在快照事务内。阻断分三类，沿引用路径逐层记录：
      - missing     ：父成果号/父修订号从未提交（父项允许晚到，晚到后重查即恢复）
      - superseded  ：父成果号存在更高已提交修订，被引用的旧修订已非现役
      - cycle       ：引用链回到链路上的某个节点
    一次评估会走完整条链并收集全部阻断，而不是遇错即停。
    """
    blockers: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    # 当前实体及其到起点的路径（含起点自身）
    current = start_row
    path = [_node_label(start_row["product_id"], start_row["revision"])]
    seen.add((start_row["product_id"], start_row["revision"]))

    while current["parent_product_id"] is not None:
        pid = current["parent_product_id"]
        rev = current["parent_revision"]
        path.append(_node_label(pid, rev))

        # 1) 父项缺失
        parent_row = db.get_revision(conn, pid, rev)
        if parent_row is None:
            blockers.append({
                "reason": REASON_MISSING,
                "reason_label": BLOCKER_LABELS[REASON_MISSING],
                "path": list(path),
                "detail": (
                    f"成果 {path[-2]} 固定引用的父项 {pid}@{rev} 尚未提交"
                ),
            })
            break  # 链路在此断开，无法继续上行

        # 2) 循环引用：父项已在当前链路上出现
        if (pid, rev) in seen:
            cycle_nodes = [_node_label(pid, rev)]
            blockers.append({
                "reason": REASON_CYCLE,
                "reason_label": BLOCKER_LABELS[REASON_CYCLE],
                "path": list(path),
                "detail": f"引用链在 {_node_label(pid, rev)} 处形成闭环",
            })
            break

        # 3) 父项已非现役：同成果号存在更高已提交修订
        active_row = db.get_active_revision(conn, pid)
        if active_row is not None and active_row["revision"] != rev:
            blockers.append({
                "reason": REASON_SUPERSEDED,
                "reason_label": BLOCKER_LABELS[REASON_SUPERSEDED],
                "path": list(path),
                "detail": (
                    f"被引用的父修订 {pid}@{rev} 已非现役，"
                    f"现役修订为 {pid}@{active_row['revision']}"
                ),
            })

        # 继续沿“实际被引用的旧修订”上行——它仍可供追溯，
        # 其自身的父链问题同样需要暴露。
        seen.add((pid, rev))
        current = parent_row

    kind = "tile" if start_row["parent_product_id"] is None else "mosaic"
    return {
        "product_id": start_row["product_id"],
        "revision": start_row["revision"],
        "is_active": start_is_active,
        "kind": kind,
        "summary": start_row["summary"],
        "parent_product_id": start_row["parent_product_id"],
        "parent_revision": start_row["parent_revision"],
        "deliverable": len(blockers) == 0,
        "blockers": blockers,
    }


def evaluate_product(
    conn: sqlite3.Connection,
    product_id: str,
    revision: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """评估某成果号；revision 为空时评估现役（最高）修订。

    成果号不存在返回 None；指定修订不存在也返回 None（由路由层区分 404 文案）。
    """
    if revision is None:
        row = db.get_active_revision(conn, product_id)
        if row is None:
            return None
        return evaluate(conn, row, start_is_active=True)

    row = db.get_revision(conn, product_id, revision)
    if row is None:
        return None
    active = db.get_active_revision(conn, product_id)
    is_active = active is not None and active["revision"] == revision
    return evaluate(conn, row, start_is_active=is_active)
