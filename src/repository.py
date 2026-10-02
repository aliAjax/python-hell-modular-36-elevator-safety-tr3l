import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
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
                CREATE TABLE IF NOT EXISTS alarm_takeovers (
                    id TEXT PRIMARY KEY,
                    alarm_id TEXT NOT NULL,
                    area TEXT NOT NULL,
                    shift_id TEXT NOT NULL,
                    dispatcher_id TEXT NOT NULL,
                    handover_id TEXT,
                    supersedes_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    invalidated_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_alarm_takeover
                    ON alarm_takeovers(alarm_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_takeover_alarm
                    ON alarm_takeovers(alarm_id, id);
                CREATE INDEX IF NOT EXISTS idx_takeover_shift
                    ON alarm_takeovers(shift_id, status);
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
        entities = self.list_entities(kind=kind)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
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

    @staticmethod
    def _takeover_from_row(row):
        return {
            "id": row["id"],
            "alarm_id": row["alarm_id"],
            "area": row["area"],
            "shift_id": row["shift_id"],
            "dispatcher_id": row["dispatcher_id"],
            "handover_id": row["handover_id"],
            "supersedes_id": row["supersedes_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "invalidated_at": row["invalidated_at"],
            "invalidated_reason": row["invalidated_reason"],
        }

    def takeover_alarm(self, alarm_id, area, shift_id, dispatcher_id, handover_id=None, supersedes_id=None):
        """Atomically attach the single active owner to an actionable alarm."""
        now = utcnow()
        takeover_id = "takeover-%s-%s" % (alarm_id, shift_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            alarm = connection.execute(
                "SELECT status FROM entities WHERE id = ? AND kind = 'alarm'",
                (alarm_id,),
            ).fetchone()
            if not alarm:
                raise NotFoundError("entity not found: " + alarm_id)
            if alarm["status"] not in ("received", "dispatched"):
                raise ConflictError("alarm is no longer open for takeover: " + alarm_id)
            shift = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'shift' AND status = 'on_duty'",
                (shift_id,),
            ).fetchone()
            if not shift:
                raise ConflictError("shift is not on duty: " + shift_id)
            shift_data = json.loads(shift["data"])
            if shift_data.get("area") != area:
                raise ConflictError("shift area does not match alarm area")

            handover_rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'handover' AND status = 'in_handover' "
                "AND json_extract(data, '$.area') = ?",
                (area,),
            ).fetchall()
            handover_row = None
            handover_data = None
            active = connection.execute(
                "SELECT * FROM alarm_takeovers WHERE alarm_id = ? AND status = 'active'",
                (alarm_id,),
            ).fetchone()
            for candidate in handover_rows:
                candidate_data = json.loads(candidate["data"])
                if active:
                    if candidate_data.get("outgoing_shift_id") == active["shift_id"]:
                        handover_row = candidate
                        handover_data = candidate_data
                        break
                elif candidate_data.get("outgoing_shift_id") != shift_id:
                    handover_row = candidate
                    handover_data = candidate_data
                    break

            effective_handover_id = None
            if active:
                if active["shift_id"] == shift_id:
                    connection.commit()
                    return self._takeover_from_row(active)
                if not handover_row:
                    raise ConflictError(
                        "alarm already taken over by shift " + active["shift_id"]
                    )
                if active["shift_id"] != handover_data.get("outgoing_shift_id"):
                    raise ConflictError(
                        "alarm already taken over by shift " + active["shift_id"]
                    )
                incoming_id = handover_data.get("incoming_shift_id")
                if incoming_id and incoming_id != shift_id:
                    raise ConflictError("alarm already assigned for takeover by shift " + incoming_id)
                effective_handover_id = handover_row["id"]
                connection.execute(
                    "UPDATE alarm_takeovers SET status = 'superseded', updated_at = ?, "
                    "invalidated_at = ?, invalidated_reason = 'superseded_in_handover' WHERE id = ?",
                    (now, now, active["id"]),
                )
            elif handover_row:
                incoming_id = handover_data.get("incoming_shift_id")
                if incoming_id and incoming_id != shift_id:
                    raise ConflictError("alarm already assigned for takeover by shift " + incoming_id)
                effective_handover_id = handover_row["id"]

            if handover_row and handover_data.get("incoming_shift_id") is None:
                handover_data["incoming_shift_id"] = shift_id
                handover_data["incoming_dispatcher_id"] = shift_data.get("dispatcher_id")
                handover_data["accepted_at"] = now
                connection.execute(
                    "UPDATE entities SET data = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(handover_data, ensure_ascii=False, sort_keys=True),
                        now,
                        handover_row["id"],
                    ),
                )

            connection.execute(
                "INSERT INTO alarm_takeovers(id, alarm_id, area, shift_id, dispatcher_id, "
                "handover_id, supersedes_id, status, created_at, updated_at, invalidated_at, invalidated_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)",
                (
                    takeover_id,
                    alarm_id,
                    area,
                    shift_id,
                    shift_data.get("dispatcher_id"),
                    effective_handover_id,
                    active["id"] if active else None,
                    now,
                    now,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            raise ConflictError("alarm already taken over: " + alarm_id)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_active_takeover(alarm_id)

    def get_active_takeover(self, alarm_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM alarm_takeovers WHERE alarm_id = ? AND status = 'active'",
                (alarm_id,),
            ).fetchone()
        return self._takeover_from_row(row) if row else None

    def list_takeovers(self, alarm_id=None, shift_id=None, status=None):
        clauses = []
        params = []
        if alarm_id:
            clauses.append("alarm_id = ?")
            params.append(alarm_id)
        if shift_id:
            clauses.append("shift_id = ?")
            params.append(shift_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alarm_takeovers" + where + " ORDER BY created_at, id",
                params,
            ).fetchall()
        return [self._takeover_from_row(row) for row in rows]

    def invalidate_active_takeover(self, alarm_id, reason):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM alarm_takeovers WHERE alarm_id = ? AND status = 'active'",
                (alarm_id,),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE alarm_takeovers SET status = 'invalidated', updated_at = ?, "
                    "invalidated_at = ?, invalidated_reason = ? WHERE id = ?",
                    (now, now, reason, row["id"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._takeover_from_row(row) if row else None

    def link_active_takeovers_to_handover(self, area, handover_id, outgoing_shift_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE alarm_takeovers SET handover_id = ?, updated_at = ? "
                "WHERE status = 'active' AND area = ? AND "
                "(shift_id = ? OR handover_id IS NULL)",
                (handover_id, now, area, outgoing_shift_id),
            )

    def refresh_handover(self, handover_id):
        """Recompute open-loop counts from the alarm snapshot and active owners."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'handover'",
                (handover_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + handover_id)
            data = json.loads(row["data"])
            alarm_ids = list(data.get("alarm_ids", []))
            active_area_alarms = connection.execute(
                "SELECT e.id FROM entities e WHERE e.kind = 'alarm' "
                "AND e.status IN ('received', 'dispatched') "
                "AND json_extract(e.data, '$.area') = ? "
                "AND ("
                "  NOT EXISTS (SELECT 1 FROM alarm_takeovers t WHERE t.alarm_id = e.id AND t.status = 'active') "
                "  OR EXISTS (SELECT 1 FROM alarm_takeovers t WHERE t.alarm_id = e.id AND t.status = 'active' AND t.shift_id = ?) "
                "  OR EXISTS (SELECT 1 FROM alarm_takeovers t WHERE t.alarm_id = e.id AND t.status = 'active' AND t.handover_id = ?)"
                ") ORDER BY e.id",
                (data.get("area"), data.get("outgoing_shift_id"), handover_id),
            ).fetchall()
            for active_alarm in active_area_alarms:
                if active_alarm["id"] not in alarm_ids:
                    alarm_ids.append(active_alarm["id"])
            data["alarm_ids"] = alarm_ids
            unclosed_ids = [alarm["id"] for alarm in active_area_alarms]

            owned_ids = []
            unowned_ids = []
            if unclosed_ids:
                placeholders = ",".join("?" for _ in unclosed_ids)
                owned_rows = connection.execute(
                    "SELECT alarm_id FROM alarm_takeovers WHERE status = 'active' "
                    "AND handover_id = ? AND shift_id != ? AND alarm_id IN (%s)" % placeholders,
                    [handover_id, data.get("outgoing_shift_id")] + unclosed_ids,
                ).fetchall()
                owned_ids = [item["alarm_id"] for item in owned_rows]
                unowned_ids = [item for item in unclosed_ids if item not in owned_ids]

            next_status = row["status"]
            if next_status == "in_handover" and not unowned_ids:
                next_status = "completed"
                data["completed_at"] = utcnow()
            data.update(
                {
                    "total_alarm_count": len(alarm_ids),
                    "unclosed_alarm_count": len(unclosed_ids),
                    "taken_over_alarm_count": len(owned_ids),
                    "pending_takeover_alarm_count": len(unowned_ids),
                    "unclosed_alarm_ids": unclosed_ids,
                    "taken_over_alarm_ids": owned_ids,
                    "pending_takeover_alarm_ids": unowned_ids,
                }
            )
            now = utcnow()
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? WHERE id = ?",
                (next_status, json.dumps(data, ensure_ascii=False, sort_keys=True), now, handover_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(handover_id)

    def resolve_alarm_with_takeover(self, entity_id, expected_version, status, data, reason):
        """Update an alarm and invalidate its current takeover in one write lock."""
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            takeover = connection.execute(
                "SELECT * FROM alarm_takeovers WHERE alarm_id = ? AND status = 'active'",
                (entity_id,),
            ).fetchone()
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            if takeover:
                connection.execute(
                    "UPDATE alarm_takeovers SET status = 'invalidated', updated_at = ?, "
                    "invalidated_at = ?, invalidated_reason = ? WHERE id = ?",
                    (now, now, reason, takeover["id"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        updated = self.get_entity(entity_id)
        return updated, self._takeover_from_row(takeover) if takeover else None

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

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
