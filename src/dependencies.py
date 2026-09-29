from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .domain import STATES

# 开工状态：进入 construction 即视为已施工，此后依据只保留不失效。
STARTED_STATES = frozenset(STATES[STATES.index("construction"):])
ACCEPTED = "accepted"


def find_path(edges: Iterable[Tuple[int, int]], source: int, target: int) -> Optional[List[int]]:
    """在有向边集合中查找 source 到 target 的节点路径，找不到返回 None。"""
    adjacency: Dict[int, List[int]] = {}
    for upstream_id, downstream_id in edges:
        adjacency.setdefault(upstream_id, []).append(downstream_id)
    if source == target:
        return [source]
    queue = deque([source])
    parents = {source: None}
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, ()):
            if nxt in parents:
                continue
            parents[nxt] = current
            if nxt == target:
                path: List[int] = []
                node: Optional[int] = target
                while node is not None:
                    path.append(node)
                    node = parents[node]
                path.reverse()
                return path
            queue.append(nxt)
    return None


def cycle_for_new_edge(edges: Sequence[Tuple[int, int]],
                       upstream_id: int, downstream_id: int) -> Optional[List[int]]:
    """若加入 upstream_id -> downstream_id 会成环，返回闭环回路（首尾相同）。

    自依赖（upstream_id == downstream_id）天然成环。
    """
    if upstream_id == downstream_id:
        return [upstream_id, downstream_id]
    path = find_path(edges, downstream_id, upstream_id)
    if path is None:
        return None
    return [upstream_id] + path


def started(status: str) -> bool:
    return status in STARTED_STATES


def construction_blockers(links: Sequence[dict]) -> List[str]:
    """汇总下游进入 construction 前的依赖障碍。

    links 中每条包含 upstream_id/status(上游状态)/shared_part/basis/edge_status。
    - 失效边：上游被驳回且下游未开工时登记的依据已失效，必须重新登记；
    - 生效边：全部上游必须已验收（accepted），否则列出未验收的共用部位。
    """
    blockers: List[str] = []
    for link in links:
        shared = link.get("shared_part") or f"上游项目{link['upstream_id']}"
        basis = link.get("basis") or ""
        if link.get("edge_status") == "invalidated":
            blockers.append(
                f"共用部位「{shared}」的依据已失效（上游项目{link['upstream_id']}被驳回：{basis}），请重新登记")
        elif link.get("status") != ACCEPTED:
            blockers.append(
                f"共用部位「{shared}」的上游项目{link['upstream_id']}尚未验收（当前{link.get('status')}）")
    return blockers
