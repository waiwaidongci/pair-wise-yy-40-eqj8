import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, NotFoundError, PermissionDenied
from src.repository import Repository
from src.rules import (DEPENDENCY_BASIS_INVALID, DEPENDENCY_BASIS_PENDING,
                       DEPENDENCY_BASIS_SATISFIED, TRANSITIONS, find_cycle)
from src.service import Service


def role_for(target):
    return {'assessed': 'assessor', 'design': 'structural_engineer',
            'construction': 'structural_engineer', 'accepted': 'review_board',
            'rejected': 'review_board'}[target]


class DependencyRulesTest(unittest.TestCase):
    def test_find_cycle_returns_loop(self):
        # 既有 A->B, B->C；登记 C->A 应成环 A?
        edges = [(1, 2), (2, 3)]
        cycle = find_cycle(edges, 3, 1)
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {1, 2, 3})

    def test_no_cycle_on_unrelated_edge(self):
        self.assertIsNone(find_cycle([(1, 2), (2, 3)], 1, 4))

    def test_self_loop_is_cycle(self):
        self.assertEqual(find_cycle([], 1, 1), [1, 1])


class DependencyWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.up = self.service.create_item(
            {"title": "上游楼", "description": "连廊先加固", "severity": "high",
             "quantity": 5, "threshold": 10, "external_ref": "DEP-UP"},
            "creator", "assessor")
        self.down = self.service.create_item(
            {"title": "下游楼", "description": "共用连廊后加固", "severity": "high",
             "quantity": 5, "threshold": 10, "external_ref": "DEP-DOWN"},
            "creator", "assessor")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, up=None, down=None, role="structural_engineer"):
        return self.service.register_dependency(
            {"upstream_item_id": up or self.up["id"],
             "downstream_item_id": down or self.down["id"],
             "shared_part": "共用连廊", "basis": "连廊支座传力计算书LC-01"},
            "engineer", role)

    def _advance(self, item, targets, close_records=False):
        current = self.repo.get_item(item["id"])
        for target in targets:
            if close_records and target in ("accepted", "rejected"):
                for rec in self.service.list_records(current["id"], "viewer"):
                    if rec["status"] == "open":
                        # 直接关闭以满足终态不变量
                        self.repo.conn.execute(
                            "UPDATE records SET status='closed' WHERE id=?", (rec["id"],))
            current = self.service.transition(
                current["id"], target, current["version"], "u", role_for(target))
        return current

    def test_register_dependency_and_basis_status(self):
        dep = self._register()
        self.assertEqual(dep["basis_status"], DEPENDENCY_BASIS_PENDING)
        self.assertEqual(dep["shared_part"], "共用连廊")
        view = self.service.dependency_view(self.down["id"], "viewer")
        self.assertEqual(len(view["upstream"]), 1)
        self.assertEqual(len(view["downstream"]), 0)
        self.assertEqual(len(view["blocked_basis"]), 1)

    def test_duplicate_relation_rejected(self):
        self._register()
        with self.assertRaises(ConflictError):
            self._register()
        self.assertEqual(len(self.service.list_dependencies("viewer")), 1)

    def test_self_dependency_rejected_with_cycle(self):
        with self.assertRaises(ConflictError) as caught:
            self._register(up=self.down["id"], down=self.down["id"])
        self.assertEqual(caught.exception.details["cycle"], [self.down["id"], self.down["id"]])

    def test_missing_project_rejected(self):
        with self.assertRaises(NotFoundError):
            self.service.register_dependency(
                {"upstream_item_id": 9999, "downstream_item_id": self.down["id"],
                 "shared_part": "连廊", "basis": "依据"}, "e", "structural_engineer")

    def test_viewer_cannot_register(self):
        with self.assertRaises(PermissionDenied):
            self._register(role="viewer")

    def test_downstream_cannot_start_before_all_upstream_accepted(self):
        self._register()
        down = self._advance(self.down, ["assessed", "design"])
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(down["id"], "construction", down["version"],
                                    "e", role_for("construction"))
        blockers = caught.exception.details["blocked_basis"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["upstream_item_id"], self.up["id"])
        self.assertEqual(blockers[0]["basis_status"], DEPENDENCY_BASIS_PENDING)

    def test_downstream_starts_after_upstream_accepted(self):
        self._register()
        self._advance(self.up, ["assessed", "design", "construction"])
        self.service.add_record(self.up["id"],
                                {"kind": "evidence", "detail": "施工证据",
                                 "status": "closed", "external_ref": "EV-UP"},
                                "e", "structural_engineer")
        self._advance(self.up, ["accepted"], close_records=True)
        dep = self.service.list_dependencies("viewer",
                                             downstream_item_id=self.down["id"])[0]
        self.assertEqual(dep["basis_status"], DEPENDENCY_BASIS_SATISFIED)
        down = self._advance(self.down, ["assessed", "design", "construction"])
        self.assertEqual(down["status"], "construction")

    def test_all_upstreams_must_be_accepted(self):
        # 两个上游：一个已验收、一个未验收，仍不能开工
        up2 = self.service.create_item(
            {"title": "上游楼2", "description": "同一传力体系", "severity": "medium",
             "quantity": 1, "threshold": 1, "external_ref": "DEP-UP2"},
            "creator", "assessor")
        self._register()
        self._register(up=up2["id"])
        self._advance(self.up, ["assessed", "design", "construction"])
        self.service.add_record(self.up["id"],
                                {"kind": "evidence", "detail": "证据",
                                 "status": "closed", "external_ref": "EV-UP-2"},
                                "e", "structural_engineer")
        self._advance(self.up, ["accepted"], close_records=True)
        down = self._advance(self.down, ["assessed", "design"])
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(down["id"], "construction", down["version"],
                                    "e", role_for("construction"))
        blocked = caught.exception.details["blocked_basis"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["upstream_item_id"], up2["id"])

    def test_rejected_upstream_invalidates_basis_for_not_started_downstream(self):
        self._register()
        self._advance(self.up, ["assessed", "rejected"])
        item = self.service.get_item(self.down["id"], "viewer")
        invalid = item["invalid_basis"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["basis_status"], DEPENDENCY_BASIS_INVALID)
        self.assertEqual(invalid[0]["upstream_item_id"], self.up["id"])
        self.assertEqual(invalid[0]["basis"], "连廊支座传力计算书LC-01")
        view = self.service.dependency_view(self.down["id"], "viewer")
        self.assertEqual(len(view["invalid_basis"]), 1)
        # 依赖关系记录仍然保留
        self.assertEqual(len(self.service.list_dependencies("viewer")), 1)
        # 依据失效同样不能开工，且阻断条目标为invalid
        down = self._advance(self.down, ["assessed", "design"])
        with self.assertRaises(ConflictError) as caught:
            self.service.transition(down["id"], "construction", down["version"],
                                    "e", role_for("construction"))
        self.assertEqual(caught.exception.details["blocked_basis"][0]["basis_status"],
                         DEPENDENCY_BASIS_INVALID)

    def test_started_downstream_keeps_records_without_invalid_warning(self):
        # 下游已开工后上游才被驳回：记录保留，不再列入失效依据告警
        self._register()
        self._advance(self.up, ["assessed", "design", "construction"])
        self.service.add_record(self.up["id"],
                                {"kind": "evidence", "detail": "证据",
                                 "status": "closed", "external_ref": "EV-UP-3"},
                                "e", "structural_engineer")
        self._advance(self.up, ["accepted"], close_records=True)
        down = self._advance(self.down, ["assessed", "design", "construction"])
        # 已施工记录保留
        self.service.add_record(down["id"],
                                {"kind": "evidence", "detail": "下游已开始施工",
                                 "status": "open", "external_ref": "EV-DOWN"},
                                "e", "structural_engineer")
        # 验收后被驳回（accepted -> rejected）
        self.service.add_record(self.up["id"],
                                {"kind": "evidence", "detail": "补充",
                                 "status": "closed", "external_ref": "EV-UP-3B"},
                                "e", "structural_engineer")
        up = self.repo.get_item(self.up["id"])
        self.service.transition(up["id"], "rejected", up["version"],
                                "b", role_for("rejected"))
        view = self.service.dependency_view(self.down["id"], "viewer")
        self.assertTrue(view["construction_started"])
        self.assertEqual(view["invalid_basis"], [])
        self.assertEqual(len(self.service.list_records(self.down["id"], "viewer")), 1)
        self.assertEqual(len(self.service.list_dependencies("viewer")), 1)

    def test_cycle_rejected_and_points_out_loop(self):
        # A(up)->B(down)，再 B->C，最后 C->A 成环，拒绝并指出回路
        third = self.service.create_item(
            {"title": "三楼", "description": "传力体系", "severity": "low",
             "quantity": 1, "threshold": 1, "external_ref": "DEP-C"},
            "creator", "assessor")
        self._register()  # up -> down
        self.service.register_dependency(
            {"upstream_item_id": self.down["id"], "downstream_item_id": third["id"],
             "shared_part": "连廊", "basis": "b1"}, "e", "structural_engineer")
        with self.assertRaises(ConflictError) as caught:
            self.service.register_dependency(
                {"upstream_item_id": third["id"], "downstream_item_id": self.up["id"],
                 "shared_part": "连廊", "basis": "b2"}, "e", "structural_engineer")
        cycle = caught.exception.details["cycle"]
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {self.up["id"], self.down["id"], third["id"]})
        # 成环边未落库
        self.assertEqual(len(self.service.list_dependencies("viewer")), 2)

    def test_concurrent_registration_cannot_bypass_cycle_check(self):
        # 两个线程并发登记互逆边 A->B 与 B->A，至多一条成功
        outcomes = []

        def register(up, down):
            try:
                self.service.register_dependency(
                    {"upstream_item_id": up, "downstream_item_id": down,
                     "shared_part": "连廊", "basis": "并发依据"},
                    "e", "structural_engineer")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        t1 = threading.Thread(target=register, args=(self.up["id"], self.down["id"]))
        t2 = threading.Thread(target=register, args=(self.down["id"], self.up["id"]))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        self.assertEqual(len(self.service.list_dependencies("viewer")), 1)

    def test_audit_chain_intact_after_dependency_register(self):
        self._register()
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
