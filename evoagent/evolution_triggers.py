"""Durable, confirmed feedback batches for Prompt evolution."""

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime

from .store import utc_now


RULE_ID = re.compile(r"^[A-Z][A-Z0-9_-]{1,79}$")


class TriggerStore:
    def __init__(self, store):
        self.store = store
        self.pg = store.__class__.__name__ == "PostgresTaskStore"
        identity = "BIGINT" if self.pg else "INTEGER"
        ddl = [
            """CREATE TABLE IF NOT EXISTS evolution_feedback (
                failure_case_id %s PRIMARY KEY, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, rule_id TEXT NOT NULL,
                review_unit TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                confirmed_by TEXT, confirmed_at TEXT, used_job_id TEXT,
                created_at TEXT NOT NULL)""" % identity,
            """CREATE UNIQUE INDEX IF NOT EXISTS evolution_one_confirmed_unit
                ON evolution_feedback(tenant_id,repository,rule_id,review_unit)
                WHERE status='confirmed'""",
            """CREATE TABLE IF NOT EXISTS evolution_jobs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                rule_id TEXT NOT NULL, source TEXT NOT NULL, request_key TEXT,
                batch_hash TEXT NOT NULL UNIQUE, case_ids TEXT NOT NULL,
                status TEXT NOT NULL, decision TEXT, reason TEXT, run_id TEXT,
                result_json TEXT,
                base_version INTEGER, base_hash TEXT, validation_hash TEXT,
                holdout_hash TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, lease_until DOUBLE PRECISION,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(tenant_id,request_key))""",
            """CREATE UNIQUE INDEX IF NOT EXISTS evolution_one_active_target
                ON evolution_jobs((1)) WHERE status IN ('pending','running','shadow_ready')""",
        ]
        with store._connect() as conn:
            for sql in ddl:
                self.run(conn, sql)

    @property
    def unresolved(self):
        return "FALSE" if self.pg else "0"

    def run(self, conn, sql, params=()):
        return conn.execute(sql.replace("?", "%s") if self.pg else sql, params)

    def job(self, row):
        if row is None:
            return None
        value = dict(row)
        value["case_ids"] = json.loads(value["case_ids"])
        value["result"] = json.loads(value.pop("result_json") or "null")
        return value

    def add_feedback(self, case_id, task, rule_id, diff=""):
        rule_id = str(rule_id or "").strip().upper()
        if not RULE_ID.fullmatch(rule_id):
            rule_id = ""
        unit = (
            "pr:%s" % task["pull_request"] if task.get("pull_request") is not None
            else "diff:%s" % hashlib.sha256((diff or task["id"]).encode()).hexdigest()
        )
        with self.store._connect() as conn:
            self.run(conn, """INSERT INTO evolution_feedback
                (failure_case_id,tenant_id,repository,rule_id,review_unit,created_at)
                VALUES (?,?,?,?,?,?)""", (case_id, task.get("tenant_id") or "default",
                task["repository"], rule_id, unit, utc_now()))

    def confirm(self, case_id, tenant_id, actor, approve=True):
        with self.store._connect() as conn:
            if self.pg:
                self.run(conn, "SELECT pg_advisory_xact_lock(716204)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            row = self.run(conn, """SELECT e.*,f.category,f.payload_json,f.resolved
                FROM evolution_feedback e JOIN failure_cases f ON f.id=e.failure_case_id
                WHERE e.failure_case_id=? AND e.tenant_id=?""", (case_id, tenant_id)).fetchone()
            if row is None:
                raise ValueError("feedback case not found")
            row = dict(row)
            if row["status"] != "pending":
                return {"case_id": case_id, "status": row["status"]}
            if approve:
                finding = json.loads(row["payload_json"]) if isinstance(row["payload_json"], str) else row["payload_json"]
                finding = (finding or {}).get("finding") or {}
                if (row["category"] != "missed_issue" or row["resolved"] or not row["rule_id"]
                        or not finding.get("path") or int(finding.get("line") or 0) <= 0):
                    raise ValueError("confirmed missed issue needs rule_id, path and added-line number")
                duplicate = self.run(conn, """SELECT failure_case_id FROM evolution_feedback
                    WHERE tenant_id=? AND repository=? AND rule_id=? AND review_unit=?
                    AND status='confirmed' LIMIT 1""", (
                    tenant_id, row["repository"], row["rule_id"], row["review_unit"])).fetchone()
                if duplicate:
                    return {"case_id": case_id, "status": "duplicate"}
            status = "confirmed" if approve else "rejected"
            self.run(conn, """UPDATE evolution_feedback SET status=?,confirmed_by=?,
                confirmed_at=? WHERE failure_case_id=?""", (status, actor, utc_now(), case_id))
            return {"case_id": case_id, "status": status}

    def groups(self, tenant_id, repository):
        with self.store._connect() as conn:
            rows = self.run(conn, """SELECT e.rule_id,COUNT(*) AS eligible
                FROM evolution_feedback e JOIN failure_cases f ON f.id=e.failure_case_id
                WHERE e.tenant_id=? AND e.repository=? AND e.status='confirmed'
                AND e.used_job_id IS NULL AND f.resolved=%s
                GROUP BY e.rule_id ORDER BY e.rule_id""" % self.unresolved,
                (tenant_id, repository)).fetchall()
        return [dict(row) for row in rows]

    def feedback(self, tenant_id, limit=100):
        with self.store._connect() as conn:
            rows = self.run(conn, """SELECT failure_case_id,repository,rule_id,status,
                confirmed_by,confirmed_at,used_job_id FROM evolution_feedback
                WHERE tenant_id=? ORDER BY failure_case_id DESC LIMIT ?""",
                (tenant_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def cases(self, tenant_id, repository, rule_id, limit, explicit=None):
        sql = """SELECT e.failure_case_id FROM evolution_feedback e
            JOIN failure_cases f ON f.id=e.failure_case_id
            WHERE e.tenant_id=? AND e.repository=? AND e.rule_id=?
            AND e.status='confirmed' AND f.resolved=%s""" % self.unresolved
        params = [tenant_id, repository, rule_id]
        sql += " AND e.used_job_id IS NULL"
        if explicit is not None:
            explicit = [int(value) for value in explicit]
            if not explicit or len(explicit) > 100:
                raise ValueError("case_ids must contain 1-100 values")
            sql += " AND e.failure_case_id IN (%s)" % ",".join("?" for _ in explicit)
            params.extend(explicit)
        sql += " ORDER BY e.failure_case_id LIMIT ?"
        params.append(limit)
        with self.store._connect() as conn:
            rows = self.run(conn, sql, params).fetchall()
        ids = [row["failure_case_id"] for row in rows]
        if explicit is not None and set(ids) != set(explicit):
            raise ValueError("case_ids include unconfirmed or out-of-scope feedback")
        return ids

    def create(self, tenant_id, repository, rule_id, source, case_ids, request_key,
               base_version, base_hash, validation_hash, holdout_hash):
        if not case_ids:
            raise ValueError("no confirmed feedback selected")
        batch_hash = hashlib.sha256(json.dumps([
            tenant_id, repository, rule_id, sorted(case_ids), source
        ], separators=(",", ":")).encode()).hexdigest()
        job_id, now = str(uuid.uuid4()), utc_now()
        with self.store._connect() as conn:
            if not self.pg:
                conn.execute("BEGIN IMMEDIATE")
            else:
                self.run(conn, "SELECT pg_advisory_xact_lock(716204)")
            if request_key:
                old = self.run(conn, "SELECT * FROM evolution_jobs WHERE tenant_id=? AND request_key=?",
                               (tenant_id, request_key)).fetchone()
                if old:
                    return self.job(old)
            old = self.run(conn, "SELECT * FROM evolution_jobs WHERE batch_hash=?", (batch_hash,)).fetchone()
            if old:
                return self.job(old)
            selected = self.run(conn, """SELECT e.failure_case_id FROM evolution_feedback e
                JOIN failure_cases f ON f.id=e.failure_case_id
                WHERE e.tenant_id=? AND e.repository=? AND e.rule_id=?
                AND e.status='confirmed' AND e.used_job_id IS NULL AND f.resolved=%s
                AND e.failure_case_id IN (%s)""" % (
                    self.unresolved, ",".join("?" for _ in case_ids)),
                [tenant_id, repository, rule_id] + case_ids).fetchall()
            if {row["failure_case_id"] for row in selected} != set(case_ids):
                raise RuntimeError("feedback batch changed before it could be reserved")
            active = self.run(conn, """SELECT id FROM evolution_jobs WHERE status IN
                ('pending','running','shadow_ready') LIMIT 1""").fetchone()
            if active:
                raise RuntimeError("an evolution candidate is already in progress")
            self.run(conn, """INSERT INTO evolution_jobs
                (id,tenant_id,repository,rule_id,source,request_key,batch_hash,
                 case_ids,status,base_version,base_hash,validation_hash,
                 holdout_hash,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?,?,?,?)""", (
                job_id, tenant_id, repository, rule_id, source, request_key,
                batch_hash, json.dumps(case_ids), base_version, base_hash,
                validation_hash, holdout_hash, now, now))
            self.run(conn, "UPDATE evolution_feedback SET used_job_id=? WHERE failure_case_id IN (%s)" %
                     ",".join("?" for _ in case_ids), [job_id] + case_ids)
        return self.get(job_id, tenant_id)

    def get(self, job_id, tenant_id=None):
        sql, params = "SELECT * FROM evolution_jobs WHERE id=?", [job_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with self.store._connect() as conn:
            row = self.run(conn, sql, params).fetchone()
        return self.job(row)

    def by_request_key(self, tenant_id, request_key):
        with self.store._connect() as conn:
            row = self.run(conn, """SELECT * FROM evolution_jobs
                WHERE tenant_id=? AND request_key=?""", (tenant_id, request_key)).fetchone()
        return self.job(row)

    def last_auto_time(self):
        with self.store._connect() as conn:
            row = self.run(conn, """SELECT created_at FROM evolution_jobs
                WHERE source='threshold' ORDER BY created_at DESC LIMIT 1""").fetchone()
        return row["created_at"] if row else None

    def active(self):
        with self.store._connect() as conn:
            row = self.run(conn, """SELECT * FROM evolution_jobs WHERE status IN
                ('pending','running','shadow_ready') LIMIT 1""").fetchone()
        return self.job(row)

    def pending(self):
        with self.store._connect() as conn:
            rows = self.run(conn, "SELECT * FROM evolution_jobs WHERE status='pending' ORDER BY created_at LIMIT 10").fetchall()
        return [self.job(row) for row in rows]

    def claim(self, job_id):
        token = uuid.uuid4().hex
        with self.store._connect() as conn:
            changed = self.run(conn, """UPDATE evolution_jobs SET status='running',
                attempts=attempts+1,lease_token=?,lease_until=?,updated_at=?
                WHERE id=? AND status='pending'""",
                (token, time.time() + 900, utc_now(), job_id)).rowcount
        return token if changed else None

    def renew(self, job_id, token):
        with self.store._connect() as conn:
            return self.run(conn, """UPDATE evolution_jobs SET lease_until=?,updated_at=?
                WHERE id=? AND status='running' AND lease_token=?""",
                (time.time() + 900, utc_now(), job_id, token)).rowcount == 1

    def finish(self, job_id, token, result=None, error=None, retry=False):
        result = result or {}
        status = "pending" if retry else (
            "failed" if error else "shadow_ready" if result.get("decision") == "shadow_ready"
            else "completed")
        with self.store._connect() as conn:
            return self.run(conn, """UPDATE evolution_jobs SET status=?,decision=?,reason=?,
                run_id=?,result_json=?,lease_token=NULL,lease_until=NULL,updated_at=?
                WHERE id=? AND status='running' AND lease_token=?""", (
                status, result.get("decision"), str(error or result.get("reason") or "")[:2000],
                result.get("run_id"), json.dumps(result), utc_now(), job_id, token)).rowcount == 1

    def recover(self):
        with self.store._connect() as conn:
            self.run(conn, """UPDATE evolution_jobs SET status='pending',lease_token=NULL,
                lease_until=NULL,updated_at=? WHERE status='running'
                AND lease_until<? AND attempts<3""", (utc_now(), time.time()))
            self.run(conn, """UPDATE evolution_jobs SET status='failed',
                reason='worker lease expired after maximum attempts',updated_at=?
                WHERE status='running' AND lease_until<? AND attempts>=3""",
                (utc_now(), time.time()))

    def dismiss(self, job_id, tenant_id):
        with self.store._connect() as conn:
            return self.run(conn, """UPDATE evolution_jobs SET status='completed',
                reason='shadow candidate dismissed',updated_at=?
                WHERE id=? AND tenant_id=? AND status='shadow_ready'""",
                (utc_now(), job_id, tenant_id)).rowcount == 1

    def retry(self, job_id, tenant_id, base_version, base_hash,
              validation_hash, holdout_hash):
        with self.store._connect() as conn:
            if not self.pg:
                conn.execute("BEGIN IMMEDIATE")
            else:
                self.run(conn, "SELECT pg_advisory_xact_lock(716204)")
            active = self.run(conn, """SELECT id FROM evolution_jobs WHERE
                status IN ('pending','running','shadow_ready') LIMIT 1""").fetchone()
            if active:
                raise RuntimeError("an evolution candidate is already in progress")
            changed = self.run(conn, """UPDATE evolution_jobs SET status='pending',
                decision=NULL,reason=NULL,run_id=NULL,result_json=NULL,attempts=0,lease_token=NULL,
                lease_until=NULL,base_version=?,base_hash=?,validation_hash=?,
                holdout_hash=?,updated_at=? WHERE id=? AND tenant_id=?
                AND status IN ('failed','completed')""", (
                base_version, base_hash, validation_hash, holdout_hash,
                utc_now(), job_id, tenant_id,
            )).rowcount
            if not changed:
                raise ValueError("only a failed or completed job can be retried")
        return self.get(job_id, tenant_id)


class EvolutionTriggers:
    def __init__(self, service):
        self.service = service
        self.settings = service.settings
        self.db = TriggerStore(service.store)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._start_lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="reviewloop-evolution", daemon=True)
        self._started = False
        if self.settings.evolution_auto_enabled:
            self._start()

    def _start(self):
        with self._start_lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._started:
            self._thread.join(timeout=2)

    def register_feedback(self, case_id, task, category, finding):
        if category == "missed_issue":
            diff = self.service.store.get_task_payload(task["id"]) or ""
            self.db.add_feedback(case_id, task, (finding or {}).get("rule_id"), diff)

    def confirm(self, case_id, tenant_id, actor, approve=True):
        result = self.db.confirm(case_id, tenant_id, actor, approve)
        self._wake.set()
        return result

    def status(self, tenant_id):
        settings = self.settings
        configured = (tenant_id == settings.evolution_target_tenant and
                      bool(settings.evolution_target_repository))
        ready = self.service.evolution.status()["ready"]
        provider = self.service.evolution.candidate_generator is not None
        last = self.db.last_auto_time()
        remaining = max(0, self.settings.evolution_auto_cooldown_seconds - int(
            time.time() - datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
        )) if last else 0
        active = self.db.active()
        return {
            "auto_enabled": settings.evolution_auto_enabled,
            "target_repository": settings.evolution_target_repository if configured else "",
            "threshold": settings.evolution_auto_threshold,
            "groups": self.db.groups(tenant_id, settings.evolution_target_repository) if configured else [],
            "active_job": {"id": active["id"], "status": active["status"]}
                if active and active["tenant_id"] == tenant_id else None,
            "cooldown_remaining_seconds": remaining,
            "ready": bool(configured and ready and provider),
            "waiting_reason": (
                "target not configured for this tenant" if not configured else
                "automatic triggering disabled" if not settings.evolution_auto_enabled else
                "model or evaluation cases not ready" if not ready or not provider else
                "candidate in progress" if active else
                "automatic trigger cooling down" if remaining else ""
            ),
        }

    def request(self, tenant_id, repository, rule_id, case_ids=None,
                request_key=None, source="manual"):
        settings = self.settings
        if tenant_id != settings.evolution_target_tenant or repository != settings.evolution_target_repository:
            raise ValueError("repository is not the configured evolution target")
        rule_id = str(rule_id or "").strip().upper()
        if not RULE_ID.fullmatch(rule_id):
            raise ValueError("valid rule_id is required")
        if request_key:
            old = self.db.by_request_key(tenant_id, request_key)
            if old:
                return old
        state = self.service.evolution.status()
        if not state["ready"] or self.service.evolution.candidate_generator is None:
            raise ValueError("LLM and independent validation/holdout cases are required")
        ids = self.db.cases(tenant_id, repository, rule_id,
                            settings.evolution_batch_max_cases, case_ids)
        if source == "threshold" and len(ids) < settings.evolution_auto_threshold:
            raise ValueError("threshold has not been reached")
        active = self.service.store.get_active_skill_version("llm-review")
        base = active["prompt"] if active else ""
        job = self.db.create(tenant_id, repository, rule_id, source, ids,
            request_key, active["version"] if active else None,
            hashlib.sha256(base.encode()).hexdigest(),
            state["validation_dataset_fingerprint"], state["holdout_dataset_fingerprint"])
        self._start()
        self._wake.set()
        return job

    def retry(self, job_id, tenant_id):
        job = self.db.get(job_id, tenant_id)
        if not job:
            raise ValueError("job not found")
        active = self.service.store.get_active_skill_version("llm-review")
        state = self.service.evolution.status()
        if not state["ready"] or self.service.evolution.candidate_generator is None:
            raise ValueError("LLM and evaluation cases are required")
        result = self.db.retry(job_id, tenant_id,
            active["version"] if active else None,
            hashlib.sha256((active["prompt"] if active else "").encode()).hexdigest(),
            state["validation_dataset_fingerprint"], state["holdout_dataset_fingerprint"])
        self._start()
        self._wake.set()
        return result

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.db.recover()
                tenant = self.settings.evolution_target_tenant
                if self.settings.evolution_auto_enabled and self.status(tenant)["ready"]:
                    repo = self.settings.evolution_target_repository
                    last = self.db.last_auto_time()
                    cooldown_over = (not last or time.time() - datetime.fromisoformat(
                        str(last).replace("Z", "+00:00")
                    ).timestamp() >= self.settings.evolution_auto_cooldown_seconds)
                    for group in self.db.groups(tenant, repo) if cooldown_over else []:
                        if group["eligible"] >= self.settings.evolution_auto_threshold:
                            try:
                                self.request(tenant, repo, group["rule_id"], source="threshold")
                            except (ValueError, RuntimeError):
                                pass
                            break
                for job in self.db.pending():
                    token = self.db.claim(job["id"])
                    if token:
                        self._execute(job, token)
            except Exception as exc:
                print("evolution trigger worker: %s" % exc)
            self._wake.wait(timeout=self.settings.evolution_scan_interval_seconds)
            self._wake.clear()

    def _execute(self, job, token):
        heartbeat_stop = threading.Event()

        def heartbeat():
            while not heartbeat_stop.wait(60):
                if not self.db.renew(job["id"], token):
                    return

        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        def check_lease():
            current = self.db.get(job["id"], job["tenant_id"])
            if not current or current["status"] != "running" or current["lease_token"] != token:
                raise RuntimeError("evolution job lease changed before candidate save")
        try:
            # Fetch exactly the persisted batch.  The old engine default of the
            # latest 100 unresolved cases would silently mix rules/repositories.
            cases = self.service.store.get_failure_cases_by_ids(job["case_ids"], job["tenant_id"])
            selected = {case["id"]: case for case in cases}
            exact = [selected[case_id] for case_id in job["case_ids"]]
            active = self.service.store.get_active_skill_version("llm-review")
            base = active["prompt"] if active else ""
            state = self.service.evolution.status()
            if (job["base_version"] != (active["version"] if active else None)
                    or job["base_hash"] != hashlib.sha256(base.encode()).hexdigest()
                    or job["validation_hash"] != state["validation_dataset_fingerprint"]
                    or job["holdout_hash"] != state["holdout_dataset_fingerprint"]):
                result = {"decision": "deferred", "reason": "baseline or evaluation cases changed"}
            else:
                result = self.service.evolution.auto_propose(
                    "llm-review", job["tenant_id"], exact, pre_save_check=check_lease)
            self.db.finish(job["id"], token, result)
        except Exception as exc:
            self.db.finish(job["id"], token, error=str(exc), retry=job["attempts"] + 1 < 3)
        finally:
            heartbeat_stop.set()
            beat.join(timeout=1)
