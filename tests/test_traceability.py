import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, RecomputeError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, downstream_facilities
from src.service import DomainService


def _fault_after(count):
    """Raise after ``count`` facilities have been processed."""
    state = {"seen": 0}

    def fault(run, facility_id):
        state["seen"] += 1
        return state["seen"] > count

    return fault


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector_a = Actor("inspector-a", "inspector")
        self.inspector_b = Actor("inspector-b", "inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def _build_chain(self):
        c1 = self.service.create(
            self.admin, "consignment",
            {"code": "C1", "origin": "Port-A", "destination": "Hub-B"},
        )
        c2 = self.service.create(
            self.admin, "consignment",
            {"code": "C2", "origin": "Hub-B", "destination": "Farm-C", "parent_id": c1["id"]},
        )
        f1 = self.service.create(self.admin, "facility", {"name": "Hub-B", "address": "1"})
        f2 = self.service.create(self.admin, "facility", {"name": "Farm-C", "address": "2"})
        f3 = self.service.create(self.admin, "facility", {"name": "Farm-C", "address": "3"})
        return c1, c2, f1, f2, f3

    def _submit(self, consignment_id, sample_id, actor=None, pest=True):
        return self.service.submit_lab_result(
            actor or self.inspector_a,
            {"sample_id": sample_id, "consignment_id": consignment_id, "pest_found": pest},
        )

    def test_trace_finds_downstream_facilities(self):
        c1, c2, f1, f2, f3 = self._build_chain()
        result = self._submit(c1["id"], "S-1")
        self.assertFalse(result["conflict"])
        runs = self.service.list_runs(consignment_id=c1["id"])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["status"], "concluded")
        self.assertEqual(set(run["data"]["facility_ids"]), {f1["id"], f2["id"], f3["id"]})
        notifications = self.service.list_notifications(consignment_id=c1["id"])
        self.assertEqual(len(notifications), 3)
        self.assertTrue(all(n["status"] == "pending" for n in notifications))
        self.assertEqual({n["data"]["facility_id"] for n in notifications},
                         {f1["id"], f2["id"], f3["id"]})

    def test_unconfirmed_voided_confirmed_preserved(self):
        c1, c2, f1, f2, f3 = self._build_chain()
        self._submit(c1["id"], "S-1")
        first = self.service.list_notifications(consignment_id=c1["id"])
        by_facility = {n["data"]["facility_id"]: n for n in first}
        # 确认 f1 的通知
        confirmed = self.service.confirm_notification(self.admin, by_facility[f1["id"]]["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        # 晚到的新结果触发重算
        self._submit(c1["id"], "S-2")
        after = self.service.list_notifications(consignment_id=c1["id"])
        # f1 保留已确认版本；f2/f3 未确认，作废重发
        f1_notifs = [n for n in after if n["data"]["facility_id"] == f1["id"]]
        f2_notifs = [n for n in after if n["data"]["facility_id"] == f2["id"]]
        f3_notifs = [n for n in after if n["data"]["facility_id"] == f3["id"]]
        self.assertEqual(len(f1_notifs), 1)
        self.assertEqual(f1_notifs[0]["status"], "confirmed")
        self.assertEqual(f1_notifs[0]["id"], by_facility[f1["id"]]["id"])
        self.assertEqual(len(f2_notifs), 2)
        self.assertEqual({n["status"] for n in f2_notifs}, {"voided", "pending"})
        self.assertEqual(len(f3_notifs), 2)
        self.assertEqual({n["status"] for n in f3_notifs}, {"voided", "pending"})

    def test_late_result_invalidates_unfinished_and_keeps_history(self):
        c1, c2, f1, f2, f3 = self._build_chain()
        # 第一次提交在 1 个种植点后中断，留下未完成（in_progress）追溯
        self.service.recompute_fault = _fault_after(1)
        with self.assertRaises(RecomputeError):
            self._submit(c1["id"], "S-1")
        self.service.recompute_fault = None
        runs = self.service.list_runs(consignment_id=c1["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "in_progress")
        # 晚到结果：未完成追溯立即作废重算
        self._submit(c1["id"], "S-late")
        runs = self.service.list_runs(consignment_id=c1["id"])
        self.assertEqual(len(runs), 2)
        statuses = {r["status"] for r in runs}
        self.assertEqual(statuses, {"invalidated", "concluded"})
        # 已有结论仍能查
        for run in runs:
            self.assertIsNotNone(self.service.get(run["id"]))
        notifications = self.service.list_notifications(consignment_id=c1["id"])
        # f1 有 1 条作废 + 1 条重发；f2/f3 各 1 条，无重复
        f1_notifs = [n for n in notifications if n["data"]["facility_id"] == f1["id"]]
        self.assertEqual(len(f1_notifs), 2)
        self.assertEqual({n["status"] for n in f1_notifs}, {"voided", "pending"})
        self.assertEqual(len([n for n in notifications if n["data"]["facility_id"] == f2["id"]]), 1)
        self.assertEqual(len([n for n in notifications if n["data"]["facility_id"] == f3["id"]]), 1)

    def test_same_sample_first_wins_later_conflicted(self):
        c1, c2, f1, f2, f3 = self._build_chain()
        first = self._submit(c1["id"], "S-dup", actor=self.inspector_a)
        self.assertFalse(first["conflict"])
        second = self._submit(c1["id"], "S-dup", actor=self.inspector_b)
        self.assertTrue(second["conflict"])
        self.assertEqual(second["conflict_with"], first["result"]["id"])
        self.assertEqual(first["result"]["status"], "effective")
        self.assertEqual(second["result"]["status"], "conflicted")
        # 后到的保留现场记录并列出冲突
        conflicts = self.service.list_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["id"], second["result"]["id"])
        self.assertEqual(conflicts[0]["data"]["inspector"], self.inspector_b.user_id)
        # 冲突结果不改变结论：只有一次追溯
        runs = self.service.list_runs(consignment_id=c1["id"])
        self.assertEqual(len(runs), 1)

    def test_recompute_retry_from_checkpoint_no_duplicates(self):
        c1, c2, f1, f2, f3 = self._build_chain()
        self.service.recompute_fault = _fault_after(2)
        with self.assertRaises(RecomputeError):
            self._submit(c1["id"], "S-1")
        self.service.recompute_fault = None
        runs = self.service.list_runs(consignment_id=c1["id"])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run["status"], "in_progress")
        self.assertEqual(len(run["data"]["processed"]), 2)
        # 从断点重试：已处理的不重复通知
        result = self.service.recompute(self.admin, c1["id"])
        self.assertEqual(result["run"]["status"], "concluded")
        notifications = self.service.list_notifications(consignment_id=c1["id"])
        self.assertEqual(len(notifications), 3)
        # 每个种植点恰好一条通知
        by_facility = {}
        for n in notifications:
            by_facility.setdefault(n["data"]["facility_id"], []).append(n)
        self.assertEqual(set(by_facility), {f1["id"], f2["id"], f3["id"]})
        self.assertTrue(all(len(v) == 1 for v in by_facility.values()))
        # f1/f2 保留原通知，未被作废重发
        for fid in (f1["id"], f2["id"]):
            self.assertEqual(by_facility[fid][0]["status"], "pending")

    def test_upgrade_fills_propagation_and_keeps_history(self):
        # 旧数据：批次缺 parent_id，种植点缺传播边
        c1 = self.service.create(
            self.admin, "consignment",
            {"code": "C1", "origin": "Port-A", "destination": "Hub-B"},
        )
        c2 = self.service.create(
            self.admin, "consignment",
            {"code": "C2", "origin": "Hub-B", "destination": "Farm-C"},
        )
        f1 = self.service.create(self.admin, "facility", {"name": "Hub-B", "address": "1"})
        f2 = self.service.create(self.admin, "facility", {"name": "Farm-C", "address": "2"})
        # 历史通知（升级前已存在）
        legacy = self.repo.create_entity(
            "legacy-notif", "notification", "pending",
            {"run_id": "legacy-run", "consignment_id": c1["id"], "facility_id": f1["id"], "version": 1},
            "admin",
        )
        before = self.rules_downstream(c1["id"])
        # 升级前仅能按目的地名匹配到 Hub-B 的 f1，父链缺失导致 Farm-C 的 f2 不可达
        self.assertEqual(before, [f1["id"]])
        added = self.service.upgrade_propagation(self.admin)
        # 按原发地/目的地补齐父批
        self.assertEqual(self.service.get(c2["id"])["data"]["parent_id"], c1["id"])
        # 补齐后下游可追溯到 Farm-C
        after = self.rules_downstream(c1["id"])
        self.assertEqual(after, [f1["id"], f2["id"]])
        # 幂等：再次升级不重复添加
        added2 = self.service.upgrade_propagation(self.admin)
        self.assertEqual(added2["parent_ids"], [])
        self.assertEqual(added2["edges"], [])
        # 历史通知仍能打开
        self.assertIsNotNone(self.service.get(legacy["id"]))

    def rules_downstream(self, start_id):
        consignments = self.service.list("consignment")
        facilities = self.service.list("facility")
        edges = self.service._propagation_edges()
        return downstream_facilities(consignments, facilities, start_id, edges)


if __name__ == "__main__":
    unittest.main()
