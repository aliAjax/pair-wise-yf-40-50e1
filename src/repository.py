import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError

SCHEMA_VERSION = 1


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0] or 0
            )
            if version < SCHEMA_VERSION:
                self._migrate_ledger_tables(connection)
                connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._backfill_legacy_links()

    @staticmethod
    def _migrate_ledger_tables(connection):
        # Upgrade from the generic entity model to a time-aware traceability
        # ledger. Only additive DDL: historical entities and notifications
        # remain readable after the upgrade.
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS propagation_links (
                id TEXT PRIMARY KEY,
                upstream TEXT NOT NULL,
                downstream TEXT NOT NULL,
                consignment_id TEXT,
                effective_from TEXT,
                effective_to TEXT,
                source TEXT NOT NULL DEFAULT 'consignment',
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_links_pair_batch
                ON propagation_links(upstream, downstream, COALESCE(consignment_id, ''));
            CREATE INDEX IF NOT EXISTS idx_links_upstream
                ON propagation_links(upstream);

            CREATE TABLE IF NOT EXISTS lab_submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id TEXT NOT NULL,
                consignment_id TEXT NOT NULL,
                pest_found INTEGER NOT NULL,
                pest_name TEXT NOT NULL DEFAULT '',
                finding TEXT NOT NULL DEFAULT '',
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                status TEXT NOT NULL,
                winner_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_effective_sample
                ON lab_submissions(sample_id) WHERE status = 'effective';
            CREATE INDEX IF NOT EXISTS idx_lab_consignment
                ON lab_submissions(consignment_id, id);

            CREATE TABLE IF NOT EXISTS trace_runs (
                id TEXT PRIMARY KEY,
                root_consignment_id TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                pest_found INTEGER NOT NULL,
                trigger_submission_id INTEGER,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                invalidated_at TEXT,
                invalidate_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_root ON trace_runs(root_consignment_id, id);

            CREATE TABLE IF NOT EXISTS trace_run_items (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                target_location TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                notification_id TEXT,
                last_error TEXT,
                PRIMARY KEY(run_id, seq)
            );

            CREATE TABLE IF NOT EXISTS run_notifications (
                run_id TEXT NOT NULL,
                target_location TEXT NOT NULL,
                notification_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, target_location)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                root_consignment_id TEXT NOT NULL,
                target_location TEXT NOT NULL,
                facility_id TEXT,
                version_no INTEGER NOT NULL,
                supersedes_id TEXT,
                superseded_by_id TEXT,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                issued_by TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                ack_note TEXT,
                voided_at TEXT,
                void_reason TEXT,
                UNIQUE(root_consignment_id, target_location, version_no)
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_root
                ON notifications(root_consignment_id, target_location, version_no);
        """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ------------------------------------------------------------------
    # Propagation links
    # ------------------------------------------------------------------
    @staticmethod
    def _link_from_row(row):
        return {
            "id": row["id"],
            "upstream": row["upstream"],
            "downstream": row["downstream"],
            "consignment_id": row["consignment_id"],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
            "source": row["source"],
            "created_at": row["created_at"],
        }

    def upsert_link(self, upstream, downstream, consignment_id=None,
                    effective_from=None, effective_to=None, source="manual",
                    link_id=None):
        link_id = link_id or str(uuid4())
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM propagation_links WHERE upstream = ? AND downstream = ? "
                "AND COALESCE(consignment_id, '') = COALESCE(?, '')",
                (upstream, downstream, consignment_id),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE propagation_links SET effective_from = COALESCE(?, effective_from), "
                    "effective_to = COALESCE(?, effective_to), source = ? WHERE id = ?",
                    (effective_from, effective_to, source, existing["id"]),
                )
                result_id = existing["id"]
            else:
                connection.execute(
                    "INSERT INTO propagation_links"
                    "(id, upstream, downstream, consignment_id, effective_from, effective_to, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (link_id, upstream, downstream, consignment_id,
                     effective_from, effective_to, source, now),
                )
                result_id = link_id
            row = connection.execute(
                "SELECT * FROM propagation_links WHERE id = ?", (result_id,)
            ).fetchone()
            connection.commit()
            return self._link_from_row(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_links(self, upstream=None):
        with self._connect() as connection:
            if upstream:
                rows = connection.execute(
                    "SELECT * FROM propagation_links WHERE upstream = ? ORDER BY effective_from, id",
                    (upstream,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM propagation_links ORDER BY effective_from, id"
                ).fetchall()
        return [self._link_from_row(row) for row in rows]

    def get_link(self, link_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM propagation_links WHERE id = ?", (link_id,)
            ).fetchone()
        return self._link_from_row(row) if row else None

    def _backfill_legacy_links(self):
        """Old records predate explicit propagation links: rebuild them from
        each consignment's origin/destination. Idempotent and additive."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, data, created_at FROM entities WHERE kind = 'consignment'"
            ).fetchall()
            added = 0
            for row in rows:
                data = json.loads(row["data"])
                origin, destination = data.get("origin"), data.get("destination")
                if not origin or not destination or origin == destination:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM propagation_links WHERE consignment_id = ?",
                    (row["id"],),
                ).fetchone()
                if exists:
                    continue
                connection.execute(
                    "INSERT INTO propagation_links"
                    "(id, upstream, downstream, consignment_id, effective_from, effective_to, source, created_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, 'migration', ?)",
                    (str(uuid4()), origin, destination, row["id"],
                     row["created_at"], row["created_at"]),
                )
                added += 1
            connection.commit()
            return added
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def backfill_links(self):
        return self._backfill_legacy_links()

    # ------------------------------------------------------------------
    # Lab submissions (first writer wins per sample id)
    # ------------------------------------------------------------------
    @staticmethod
    def _submission_from_row(row):
        return {
            "id": int(row["id"]),
            "sample_id": row["sample_id"],
            "consignment_id": row["consignment_id"],
            "pest_found": bool(row["pest_found"]),
            "pest_name": row["pest_name"],
            "finding": row["finding"],
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "effective_at": row["effective_at"],
            "status": row["status"],
            "winner_id": row["winner_id"],
            "created_at": row["created_at"],
        }

    def submit_lab(self, *, sample_id, consignment_id, pest_found, pest_name,
                   finding, submitted_by, submitted_at, effective_at):
        """Atomically record a lab result.

        The first submission for a sample id becomes effective; every later
        one is retained with status 'conflict'. Returns (record, outcome,
        winner) where outcome is 'effective' or 'conflict'.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT * FROM lab_submissions WHERE sample_id = ? AND consignment_id = ? "
                "AND submitted_by = ? AND pest_found = ? ORDER BY id",
                (sample_id, consignment_id, int(bool(pest_found)), submitted_by),
            ).fetchone()
            if duplicate:
                connection.commit()
                outcome = duplicate["status"]
                winner = None
                if outcome == "conflict":
                    winner_row = connection.execute(
                        "SELECT * FROM lab_submissions WHERE id = ?",
                        (duplicate["winner_id"],),
                    ).fetchone()
                    winner = self._submission_from_row(winner_row) if winner_row else None
                return self._submission_from_row(duplicate), outcome, winner

            winner_row = connection.execute(
                "SELECT * FROM lab_submissions WHERE sample_id = ? AND status = 'effective'",
                (sample_id,),
            ).fetchone()
            status = "conflict" if winner_row else "effective"
            cursor = connection.execute(
                "INSERT INTO lab_submissions"
                "(sample_id, consignment_id, pest_found, pest_name, finding, "
                "submitted_by, submitted_at, effective_at, status, winner_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sample_id, consignment_id, int(bool(pest_found)), pest_name, finding,
                 submitted_by, submitted_at, effective_at, status,
                 winner_row["id"] if winner_row else None, utcnow()),
            )
            submission_id = cursor.lastrowid
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get_submission(submission_id)
        winner = self._submission_from_row(winner_row) if winner_row else None
        return record, status, winner

    def get_submission(self, submission_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM lab_submissions WHERE id = ?", (submission_id,)
            ).fetchone()
        return self._submission_from_row(row) if row else None

    def list_submissions(self, consignment_id=None, sample_id=None, status=None):
        clauses = []
        params = []
        if consignment_id:
            clauses.append("consignment_id = ?")
            params.append(consignment_id)
        if sample_id:
            clauses.append("sample_id = ?")
            params.append(sample_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM lab_submissions" + where + " ORDER BY id", params
            ).fetchall()
        return [self._submission_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Trace runs (the recomputed ledger)
    # ------------------------------------------------------------------
    @staticmethod
    def _run_from_row(row):
        return {
            "id": row["id"],
            "root_consignment_id": row["root_consignment_id"],
            "effective_at": row["effective_at"],
            "generation": int(row["generation"]),
            "status": row["status"],
            "pest_found": bool(row["pest_found"]),
            "trigger_submission_id": row["trigger_submission_id"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidate_reason": row["invalidate_reason"],
        }

    def create_run(self, *, run_id, root_consignment_id, effective_at, pest_found,
                   trigger_submission_id, items):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            generation_row = connection.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 FROM trace_runs WHERE root_consignment_id = ?",
                (root_consignment_id,),
            ).fetchone()
            generation = int(generation_row[0])
            status = "pending"
            connection.execute(
                "INSERT INTO trace_runs(id, root_consignment_id, effective_at, generation, status, "
                "pest_found, trigger_submission_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, root_consignment_id, effective_at, generation, status,
                 int(bool(pest_found)), trigger_submission_id, now, now),
            )
            for seq, item in enumerate(items, start=1):
                connection.execute(
                    "INSERT INTO trace_run_items(run_id, seq, target_location, detail, status) "
                    "VALUES (?, ?, ?, ?, 'pending')",
                    (run_id, seq, item["target_location"],
                     json.dumps(item.get("detail", {}), ensure_ascii=False, sort_keys=True)),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_run(run_id)

    def get_run(self, run_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trace_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row else None

    def list_runs(self, root_consignment_id=None, status=None):
        clauses = []
        params = []
        if root_consignment_id:
            clauses.append("root_consignment_id = ?")
            params.append(root_consignment_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trace_runs" + where + " ORDER BY root_consignment_id, generation, id",
                params,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _item_from_row(row):
        return {
            "seq": int(row["seq"]),
            "target_location": row["target_location"],
            "detail": json.loads(row["detail"] or "{}"),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "notification_id": row["notification_id"],
            "last_error": row["last_error"],
        }

    def list_run_items(self, run_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trace_run_items WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def next_pending_item(self, run_id):
        """Claim the next breakpoint: the oldest pending item gets one more
        attempt. Returns None when every item has been handled."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM trace_run_items WHERE run_id = ? AND status = 'pending' "
                "ORDER BY seq LIMIT 1",
                (run_id,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            connection.execute(
                "UPDATE trace_run_items SET attempts = attempts + 1 "
                "WHERE run_id = ? AND seq = ?",
                (run_id, row["seq"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        item = self._item_from_row(row)
        item["attempts"] += 1
        return item

    def mark_item(self, run_id, seq, status, notification_id=None, error=None):
        with self._connect() as connection:
            connection.execute(
                "UPDATE trace_run_items SET status = ?, notification_id = COALESCE(?, notification_id), "
                "last_error = ? WHERE run_id = ? AND seq = ?",
                (status, notification_id, error, run_id, seq),
            )

    def mark_run(self, run_id, status, error=None, completed=False):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE trace_runs SET status = ?, error = ?, updated_at = ?, "
                "completed_at = COALESCE(completed_at, ?) WHERE id = ?",
                (status, error, now, now if completed else None, run_id),
            )

    def invalidate_open_runs(self, root_consignment_id, reason, at=None,
                             void_unconfirmed=False):
        """Invalidate every unfinished run for a root batch. Unfinished runs'
        unconfirmed notices are always voided; a later conclusion (e.g. a
        negative retest) can also void unconfirmed notices from finished runs.
        Acknowledged versions are never touched and remain queryable."""
        at = at or utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id FROM trace_runs WHERE root_consignment_id = ? "
                "AND status IN ('pending', 'running', 'failed') ORDER BY id",
                (root_consignment_id,),
            ).fetchall()
            run_ids = [row["id"] for row in rows]
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                connection.execute(
                    "UPDATE trace_runs SET status = 'invalidated', invalidated_at = ?, "
                    "invalidate_reason = ?, updated_at = ? WHERE id IN (%s)" % placeholders,
                    [at, reason, at] + run_ids,
                )
                connection.execute(
                    "UPDATE notifications SET status = 'voided', voided_at = ?, "
                    "void_reason = 'recalculated' WHERE status = 'issued' AND id IN ("
                    "SELECT notification_id FROM run_notifications WHERE run_id IN (%s))"
                    % placeholders,
                    [at] + run_ids,
                )
            if void_unconfirmed:
                connection.execute(
                    "UPDATE notifications SET status = 'voided', voided_at = ?, "
                    "void_reason = ? WHERE status = 'issued' "
                    "AND root_consignment_id = ?",
                    (at, reason, root_consignment_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return run_ids

    # ------------------------------------------------------------------
    # Notifications (versioned; acknowledged versions are immutable)
    # ------------------------------------------------------------------
    @staticmethod
    def _notification_from_row(row):
        return {
            "id": row["id"],
            "root_consignment_id": row["root_consignment_id"],
            "target_location": row["target_location"],
            "facility_id": row["facility_id"],
            "version_no": int(row["version_no"]),
            "supersedes_id": row["supersedes_id"],
            "superseded_by_id": row["superseded_by_id"],
            "status": row["status"],
            "payload": json.loads(row["payload"]),
            "issued_by": row["issued_by"],
            "issued_at": row["issued_at"],
            "acknowledged_at": row["acknowledged_at"],
            "acknowledged_by": row["acknowledged_by"],
            "ack_note": row["ack_note"],
            "voided_at": row["voided_at"],
            "void_reason": row["void_reason"],
        }

    def get_notification(self, notification_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        return self._notification_from_row(row) if row else None

    def list_notifications(self, root_consignment_id=None, target_location=None,
                           status=None, include_void=True):
        clauses = []
        params = []
        if root_consignment_id:
            clauses.append("root_consignment_id = ?")
            params.append(root_consignment_id)
        if target_location:
            clauses.append("target_location = ?")
            params.append(target_location)
        if status:
            clauses.append("status = ?")
            params.append(status)
        elif not include_void:
            clauses.append("status != 'voided'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notifications" + where
                + " ORDER BY root_consignment_id, target_location, version_no",
                params,
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

    def issue_notification(self, *, run_id, root_consignment_id, target_location,
                           facility_id, payload, issued_by, at=None):
        """Issue one ledger notification for a run target.

        Acknowledged versions are kept as-is (outcome 'retained'); unconfirmed
        versions are voided and replaced with a new version ('reissued');
        first notices are created at version 1 ('issued'). The run/target
        ledger row makes retries idempotent.
        """
        at = at or utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            done = connection.execute(
                "SELECT notification_id FROM run_notifications WHERE run_id = ? AND target_location = ?",
                (run_id, target_location),
            ).fetchone()
            if done:
                row = connection.execute(
                    "SELECT * FROM notifications WHERE id = ?",
                    (done["notification_id"],),
                ).fetchone()
                connection.commit()
                return self._notification_from_row(row), "duplicate"

            latest = connection.execute(
                "SELECT * FROM notifications WHERE root_consignment_id = ? AND target_location = ? "
                "ORDER BY version_no DESC LIMIT 1",
                (root_consignment_id, target_location),
            ).fetchone()

            if latest is not None and latest["status"] == "acknowledged":
                connection.execute(
                    "INSERT INTO run_notifications(run_id, target_location, notification_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, target_location, latest["id"], at),
                )
                connection.commit()
                return self._notification_from_row(latest), "retained"

            if latest is not None:
                new_version = int(latest["version_no"]) + 1
                supersedes_id = latest["id"]
                if latest["status"] == "issued":
                    connection.execute(
                        "UPDATE notifications SET status = 'voided', voided_at = ?, "
                        "void_reason = COALESCE(void_reason, 'recalculated') WHERE id = ?",
                        (at, latest["id"]),
                    )
            else:
                new_version = 1
                supersedes_id = None

            notification_id = str(uuid4())
            connection.execute(
                "INSERT INTO notifications(id, root_consignment_id, target_location, facility_id, "
                "version_no, supersedes_id, status, payload, issued_by, issued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'issued', ?, ?, ?)",
                (notification_id, root_consignment_id, target_location, facility_id,
                 new_version, supersedes_id,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), issued_by, at),
            )
            if supersedes_id:
                connection.execute(
                    "UPDATE notifications SET superseded_by_id = ? WHERE id = ?",
                    (notification_id, supersedes_id),
                )
            connection.execute(
                "INSERT INTO run_notifications(run_id, target_location, notification_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, target_location, notification_id, at),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_notification(notification_id), (
            "issued" if new_version == 1 else "reissued"
        )

    def acknowledge_notification(self, notification_id, acknowledged_by, note, at=None):
        at = at or utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("notification not found: " + notification_id)
            if row["status"] == "voided":
                raise ConflictError("notification %s is voided; a newer version exists" % notification_id)
            if row["status"] == "acknowledged":
                connection.commit()
                return self._notification_from_row(row)
            connection.execute(
                "UPDATE notifications SET status = 'acknowledged', acknowledged_at = ?, "
                "acknowledged_by = ?, ack_note = ? WHERE id = ?",
                (at, acknowledged_by, note, notification_id),
            )
            connection.commit()
        except (ConflictError, NotFoundError):
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_notification(notification_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
