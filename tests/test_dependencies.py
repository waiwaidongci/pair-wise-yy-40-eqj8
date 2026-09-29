import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (ConflictError, CycleError, PermissionDenied,
                        ValidationError)
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class DependencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, ref):
        return self.service.create_item(
            {"title": ref, "description": "building reinforcement",
             "severity": "medium", "quantity": 1, "threshold": 2,
             "external_ref": ref}, "creator", "assessor")

    def _advance(self, item, stop_before="construction"):
        """把项目从 proposed 推进到 stop_before 状态（含）。"""
        current = item
        for target in STATES[1:STATES.index(stop_before) + 1]:
            if target == "construction":
                self.service.add_record(
                    current["id"],
                    {"kind": "evidence", "detail": "closed before terminal",
                     "status": "closed"}, "rec", "assessor")
            current = self.service.transition(
                current["id"], target, current["version"], "r",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_edge_keeps_single_relation(self):
        a, b = self._item("A"), self._item("B")
        payload = {"upstream_id": a["id"], "shared_part": "连廊L1",
                   "basis": "结施-连廊-03 图3.2"}
        edge = self.service.register_dependency(b["id"], payload, "eng",
                                                "structural_engineer")
        self.assertEqual(edge["status"], "active")
        self.assertEqual(edge["upstream_id"], a["id"])
        self.assertEqual(edge["downstream_id"], b["id"])
        with self.assertRaises(ConflictError):
            self.service.register_dependency(b["id"], payload, "eng",
                                             "structural_engineer")
        edges = self.service.list_dependencies(b["id"], "viewer", "upstream")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["upstream_title"], "A")

    def test_self_dependency_is_a_cycle(self):
        a = self._item("S")
        with self.assertRaises(CycleError) as ctx:
            self.service.register_dependency(
                a["id"], {"upstream_id": a["id"], "shared_part": "x",
                          "basis": "y"}, "eng", "structural_engineer")
        self.assertEqual(ctx.exception.cycle, [a["id"], a["id"]])

    def test_cycle_rejected_with_loop(self):
        a, b, c = self._item("CA"), self._item("CB"), self._item("CC")
        reg = lambda down, up: self.service.register_dependency(
            down, {"upstream_id": up, "shared_part": "连廊",
                   "basis": "结施-共用-01"}, "eng", "structural_engineer")
        reg(b["id"], a["id"])
        reg(c["id"], b["id"])
        with self.assertRaises(CycleError) as ctx:
            reg(a["id"], c["id"])
        self.assertEqual(ctx.exception.cycle, [c["id"], a["id"], b["id"], c["id"]])
        # 拒绝后图中仍只有两条边
        self.assertEqual(len(self.repo.list_dependencies(status="active")), 2)

    def test_downstream_waits_for_all_upstream_accepted(self):
        a, b, c = self._item("UA"), self._item("UB"), self._item("DC")
        for up in (a, b):
            self.service.register_dependency(
                c["id"], {"upstream_id": up["id"], "shared_part": "共用柱",
                          "basis": "结施-柱-07"}, "eng",
                "structural_engineer")
        self._advance(a, "accepted")
        c_design = self._advance(c, "design")
        # 仅 A 验收，B 未验收 => 不能开工
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(c_design["id"], "construction",
                                    c_design["version"], "r",
                                    "structural_engineer")
        self.assertIn(f"上游项目{b['id']}尚未验收", str(ctx.exception))
        self._advance(b, "accepted")
        # 全部上游验收后可开工
        started = self.service.transition(c_design["id"], "construction",
                                          c_design["version"], "r",
                                          "structural_engineer")
        self.assertEqual(started["status"], "construction")

    def test_rejected_upstream_invalidates_basis_of_unstarted_downstream(self):
        a, started_down, waiting_down = (self._item("UP"),
                                         self._item("D1"), self._item("D2"))
        reg = lambda down: self.service.register_dependency(
            down["id"], {"upstream_id": a["id"], "shared_part": "连廊L2",
                         "basis": "结施-连廊-09"}, "eng",
            "structural_engineer")
        reg(started_down)
        reg(waiting_down)
        # 上游先验收，D1 随后开工（施工记录保留）
        self._advance(a, "accepted")
        d1 = self._advance(started_down, "construction")
        self.assertEqual(d1["status"], "construction")
        # D2 仍在 design，尚未开工
        d2 = self._advance(waiting_down, "design")
        # 已验收的上游被驳回
        a_accepted = self.service.get_item(a["id"], "viewer")
        self.service.transition(a_accepted["id"], "rejected",
                                a_accepted["version"], "r", "review_board")
        # 已开工的 D1 记录保留为 active
        kept = self.repo.list_dependencies(started_down["id"], "upstream",
                                           "active")
        self.assertEqual(len(kept), 1)
        # 未开工的 D2 依据失效，并可在失效依据清单中查到
        invalidated = self.service.list_dependencies(waiting_down["id"],
                                                     "viewer", "upstream",
                                                     "invalidated")
        self.assertEqual(len(invalidated), 1)
        self.assertEqual(invalidated[0]["shared_part"], "连廊L2")
        # 失效依据会阻止 D2 开工
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(d2["id"], "construction",
                                    d2["version"], "r",
                                    "structural_engineer")
        self.assertIn("依据已失效", str(ctx.exception))

    def test_reregister_invalidated_edge_reactivates(self):
        a, b = self._item("R1"), self._item("R2")
        self.service.register_dependency(
            b["id"], {"upstream_id": a["id"], "shared_part": "连廊",
                      "basis": "旧依据"}, "eng", "structural_engineer")
        self._advance(a, "accepted")
        a_now = self.service.get_item(a["id"], "viewer")
        self.service.transition(a_now["id"], "rejected",
                                a_now["version"], "r", "review_board")
        self.assertEqual(len(self.repo.list_dependencies(status="active")), 0)
        # 同一关系重新登记（补新依据），不新增重复行且重新生效
        edge = self.service.register_dependency(
            b["id"], {"upstream_id": a["id"], "shared_part": "连廊",
                      "basis": "新依据：复查后恢复"}, "eng",
            "structural_engineer")
        self.assertEqual(edge["status"], "active")
        self.assertEqual(edge["basis"], "新依据：复查后恢复")
        self.assertEqual(len(self.repo.list_dependencies()), 1)

    def test_permission_and_validation(self):
        a, b = self._item("PA"), self._item("PB")
        with self.assertRaises(PermissionDenied):
            self.service.register_dependency(
                b["id"], {"upstream_id": a["id"], "shared_part": "x",
                          "basis": "y"}, "eng", "viewer")
        with self.assertRaises(ValidationError):
            self.service.register_dependency(
                b["id"], {"upstream_id": "1", "shared_part": "x",
                          "basis": "y"}, "eng", "structural_engineer")
        with self.assertRaises(ValidationError):
            self.service.register_dependency(
                b["id"], {"upstream_id": a["id"], "shared_part": "  ",
                          "basis": "y"}, "eng", "structural_engineer")

    def test_concurrent_edges_cannot_bypass_cycle_check(self):
        a, b, c = self._item("XA"), self._item("XB"), self._item("XC")
        reg = lambda down, up: self.service.register_dependency(
            down, {"upstream_id": up, "shared_part": "连廊",
                   "basis": "并发依据"}, "eng", "structural_engineer")
        reg(b["id"], a["id"])
        errors = []

        def task(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - 收集并发结果
                errors.append(exc)

        barrier = threading.Barrier(2)

        def add_b_to_c():
            barrier.wait()
            reg(c["id"], b["id"])

        def add_c_to_a():
            barrier.wait()
            reg(a["id"], c["id"])

        threads = [threading.Thread(target=task, args=(add_b_to_c,)),
                   threading.Thread(target=task, args=(add_c_to_a,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 两种成功顺序里必有一个成环被拒，最终图仍是两条边的无环链
        self.assertTrue(any(isinstance(e, CycleError) for e in errors),
                        f"应当至少拒绝一次成环，实际错误：{errors}")
        active = self.repo.list_dependencies(status="active")
        self.assertEqual(len(active), 2)

    def test_concurrent_duplicate_relation_stays_single(self):
        a, b = self._item("DA"), self._item("DB")
        errors = []

        def task():
            try:
                self.service.register_dependency(
                    b["id"], {"upstream_id": a["id"], "shared_part": "连廊",
                              "basis": "重复登记"}, "eng",
                    "structural_engineer")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        barrier = threading.Barrier(4)

        def runner():
            barrier.wait()
            task()

        threads = [threading.Thread(target=runner) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.repo.list_dependencies()), 1)
        self.assertTrue(all(isinstance(e, ConflictError) for e in errors))


if __name__ == "__main__":
    unittest.main()
