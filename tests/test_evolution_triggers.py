"""Meaningful trigger invariants without a paid model or external services."""

import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from evoagent.evolution_triggers import EvolutionTriggers, TriggerStore
from evoagent.store import TaskStore


class EvolutionTriggerStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.triggers = TriggerStore(self.store)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except PermissionError:
            pass

    def add(self, number, rule="AUTHZ-01", repository="demo/api", tenant="default",
            pr=None):
        task_id = "%08d-0000-0000-0000-000000000000" % number
        self.store.create(task_id, repository, number if pr is None else pr, {}, tenant)
        case_id = self.store.record_failure_case(task_id, "missed_issue", {
            "finding": {"rule_id": rule, "path": "app.py", "line": 2},
            "note": "confirmed missing authorization check",
        })
        task = self.store.get(task_id, tenant)
        self.triggers.add_feedback(case_id, task, rule)
        return case_id

    def test_five_independent_cases_and_duplicate_pr(self):
        ids = [self.add(i) for i in range(1, 5)]
        duplicate = self.add(9, pr=1)
        for case_id in ids:
            self.assertEqual("confirmed", self.triggers.confirm(case_id, "default", "admin")["status"])
        self.assertEqual("duplicate", self.triggers.confirm(duplicate, "default", "admin")["status"])
        self.assertEqual(4, self.triggers.groups("default", "demo/api")[0]["eligible"])
        fifth = self.add(5)
        self.triggers.confirm(fifth, "default", "admin")
        self.assertEqual(5, self.triggers.groups("default", "demo/api")[0]["eligible"])

    def test_scope_idempotency_and_single_active_job(self):
        ids = [self.add(i) for i in range(1, 6)]
        for case_id in ids:
            self.triggers.confirm(case_id, "default", "admin")
        self.assertEqual([], self.triggers.cases("other", "demo/api", "AUTHZ-01", 20))
        self.assertEqual([], self.triggers.cases("default", "other/api", "AUTHZ-01", 20))
        picked = self.triggers.cases("default", "demo/api", "AUTHZ-01", 20)
        self.assertEqual(ids, picked)
        job = self.triggers.create("default", "demo/api", "AUTHZ-01", "threshold",
                                   picked, None, None, "base", "validation", "holdout")
        same = self.triggers.create("default", "demo/api", "AUTHZ-01", "threshold",
                                    picked, None, None, "base", "validation", "holdout")
        self.assertEqual(job["id"], same["id"])
        self.assertEqual([], self.triggers.cases("default", "demo/api", "AUTHZ-01", 20))
        self.assertTrue(self.triggers.claim(job["id"]))
        token = self.triggers.get(job["id"])["lease_token"]
        self.assertFalse(self.triggers.finish(job["id"], "stale", {"decision": "shadow_ready"}))
        self.assertTrue(self.triggers.finish(job["id"], token, {"decision": "shadow_ready", "run_id": "run-1"}))
        self.assertEqual("run-1", self.triggers.get(job["id"])["result"]["run_id"])
        self.assertTrue(self.triggers.dismiss(job["id"], "default"))

    def test_manual_can_start_below_threshold(self):
        class FakeEvolution:
            candidate_generator = object()

            def status(self):
                return {"ready": True, "validation_dataset_fingerprint": "v",
                        "holdout_dataset_fingerprint": "h"}

            def auto_propose(self, *_args, **_kwargs):
                return {"decision": "deferred", "reason": "controlled fake"}

        settings = SimpleNamespace(
            evolution_auto_enabled=False, evolution_auto_threshold=5,
            evolution_target_tenant="default", evolution_target_repository="demo/api",
            evolution_scan_interval_seconds=1, evolution_batch_max_cases=20,
            evolution_auto_cooldown_seconds=0,
        )
        manager = EvolutionTriggers(SimpleNamespace(
            store=self.store, settings=settings, evolution=FakeEvolution()))
        try:
            case_id = self.add(1)
            manager.confirm(case_id, "default", "admin")
            job = manager.request("default", "demo/api", "AUTHZ-01", request_key="manual-1")
            self.assertEqual("manual", job["source"])
            self.assertEqual([case_id], job["case_ids"])
            self.assertEqual(job["id"], manager.request(
                "default", "demo/api", "AUTHZ-01", request_key="manual-1")["id"])
        finally:
            manager.close()

    def test_two_process_claims_only_one_job(self):
        ids = [self.add(i) for i in range(1, 6)]
        for case_id in ids:
            self.triggers.confirm(case_id, "default", "admin")
        jobs, errors = [], []

        def submit():
            try:
                jobs.append(self.triggers.create("default", "demo/api", "AUTHZ-01",
                    "threshold", ids, None, None, "base", "validation", "holdout"))
            except RuntimeError as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(2, len(jobs) + len(errors))
        self.assertEqual(1, len({job["id"] for job in jobs}))

    def test_auto_trigger_and_manual_use_exact_batch(self):
        seen = []

        class FakeEvolution:
            candidate_generator = object()

            def status(self):
                return {"ready": True, "validation_dataset_fingerprint": "v",
                        "holdout_dataset_fingerprint": "h"}

            def auto_propose(self, _name, _tenant, cases, pre_save_check=None):
                pre_save_check()
                seen.append([item["id"] for item in cases])
                return {"decision": "rejected", "reason": "controlled fake"}

        settings = SimpleNamespace(
            evolution_auto_enabled=True, evolution_auto_threshold=5,
            evolution_target_tenant="default", evolution_target_repository="demo/api",
            evolution_scan_interval_seconds=1, evolution_batch_max_cases=20,
            evolution_auto_cooldown_seconds=0,
        )
        manager = EvolutionTriggers(SimpleNamespace(
            store=self.store, settings=settings, evolution=FakeEvolution()))
        try:
            ids = [self.add(i) for i in range(1, 5)]
            for case_id in ids:
                manager.confirm(case_id, "default", "admin")
            time.sleep(1.2)
            self.assertEqual([], seen)
            fifth = self.add(5)
            manager.confirm(fifth, "default", "admin")
            deadline = time.time() + 5
            while not seen and time.time() < deadline:
                time.sleep(.05)
            self.assertEqual([[*ids, fifth]], seen)
            self.assertEqual([], manager.db.cases("default", "demo/api", "AUTHZ-01", 20))
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
