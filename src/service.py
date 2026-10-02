import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _now(self):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _require_shift_actor(self, actor, shift):
        if actor.role not in ("admin", "supervisor") and actor.user_id != shift["data"].get("dispatcher_id"):
            raise PermissionDenied("shift belongs to another dispatcher")

    def _active_area_alarms(self, area):
        return [
            alarm
            for alarm in self.repository.list_entities(kind="alarm")
            if alarm["status"] in ("received", "dispatched")
            and (alarm["data"].get("area") == area)
        ]

    def _find_open_handover(self, shift_id):
        handovers = [
            item
            for item in self.repository.list_entities(kind="handover")
            if item["data"].get("outgoing_shift_id") == shift_id
            and item["status"] == "in_handover"
        ]
        return handovers[0] if handovers else None

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "shift":
            if action == "begin_handover":
                return self.begin_handover(actor, entity_id, expected_version)
            if action == "end_shift":
                return self.end_shift(actor, entity_id, expected_version)
            if action == "start_duty":
                self._require_shift_actor(actor, entity)
        if entity["kind"] == "handover":
            raise InvalidTransition("handover completes automatically after all alarms are taken over")
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        is_alarm_invalidation = entity["kind"] == "alarm" and action in ("mark_false", "resolve")
        if is_alarm_invalidation:
            updated, invalidated = self.repository.resolve_alarm_with_takeover(
                entity_id, expected, next_status, merged, "false_alarm" if action == "mark_false" else "resolved"
            )
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
            invalidated = None
        detail = {"patch": patch}
        if invalidated:
            detail["invalidated_takeover_id"] = invalidated["id"]
            if invalidated.get("handover_id"):
                refreshed = self.repository.refresh_handover(invalidated["handover_id"])
                detail["handover"] = {"id": refreshed["id"], "status": refreshed["status"],
                                      "unclosed_alarm_count": refreshed["data"]["unclosed_alarm_count"]}
                if refreshed["status"] == "completed":
                    self._end_outgoing_shift(actor, refreshed)
        self._resolve_alarm_escalations(actor, entity_id, action)
        self.audit.record(entity_id, actor, action, entity["status"], updated["status"], detail)
        return updated

    def begin_handover(self, actor, shift_id, expected_version=None):
        shift = self.repository.get_entity(shift_id)
        if not shift or shift["kind"] != "shift":
            raise NotFoundError("shift not found: " + shift_id)
        self._require_shift_actor(actor, shift)
        if shift["status"] == "ended":
            raise InvalidTransition("cannot hand over an ended shift")
        existing = self._find_open_handover(shift_id)
        if existing:
            return existing
        if shift["status"] != "on_duty":
            raise InvalidTransition("cannot begin handover from status " + shift["status"])
        expected = int(expected_version) if expected_version is not None else shift["version"]
        alarms = []
        for alarm in self._active_area_alarms(shift["data"]["area"]):
            active = self.repository.get_active_takeover(alarm["id"])
            if not active or active["shift_id"] == shift_id:
                alarms.append(alarm)
        handover_id = "handover-" + shift_id
        data = {
            "area": shift["data"]["area"],
            "outgoing_shift_id": shift_id,
            "outgoing_dispatcher_id": shift["data"]["dispatcher_id"],
            "alarm_ids": [alarm["id"] for alarm in alarms],
            "created_at": self._now(),
        }
        self.repository.update_entity(shift_id, expected, "handing_over", shift["data"])
        try:
            self.repository.create_entity(
                handover_id, "handover", "in_handover", data, actor.user_id
            )
            self.repository.link_active_takeovers_to_handover(
                shift["data"]["area"], handover_id, shift_id
            )
            handover = self.repository.refresh_handover(handover_id)
        except Exception:
            current = self.repository.get_entity(shift_id)
            if current and current["status"] == "handing_over":
                self.repository.update_entity(shift_id, current["version"], "on_duty", current["data"])
            raise
        self.audit.record(shift_id, actor, "begin_handover", "on_duty", "handing_over",
                          {"handover_id": handover_id, "alarm_count": len(alarms)})
        self.audit.record(handover_id, actor, "create", None, handover["status"],
                          {"area": data["area"], "alarm_ids": data["alarm_ids"]})
        if handover["status"] == "completed":
            handover = self._end_outgoing_shift(actor, handover)
        return handover

    def takeover_alarm(self, actor, alarm_id, shift_id):
        alarm = self.repository.get_entity(alarm_id)
        if not alarm or alarm["kind"] != "alarm":
            raise NotFoundError("alarm not found: " + alarm_id)
        shift = self.repository.get_entity(shift_id)
        if not shift or shift["kind"] != "shift":
            raise NotFoundError("shift not found: " + shift_id)
        if actor.role not in ("admin", "supervisor") and actor.user_id != shift["data"].get("dispatcher_id"):
            raise PermissionDenied("cannot take over alarms for another dispatcher's shift")
        if shift["status"] != "on_duty":
            raise InvalidTransition("only an on-duty shift can take over alarms")
        alarm_area = alarm["data"].get("area")
        if alarm_area != shift["data"].get("area"):
            raise ValidationError("alarm area does not belong to the on-duty shift area")
        if alarm["status"] not in ("received", "dispatched"):
            raise ConflictError("alarm is not open for takeover")

        takeover = self.repository.takeover_alarm(
            alarm_id,
            alarm_area,
            shift_id,
            shift["data"]["dispatcher_id"],
        )
        self.audit.record(
            alarm_id,
            actor,
            "takeover",
            alarm["status"],
            alarm["status"],
            {"takeover_id": takeover["id"], "shift_id": shift_id,
             "handover_id": takeover.get("handover_id"), "supersedes_id": takeover.get("supersedes_id")},
        )
        if takeover.get("handover_id"):
            handover = self.repository.refresh_handover(takeover["handover_id"])
            self.audit.record(handover["id"], actor, "takeover_alarm", handover["status"], handover["status"],
                              {"alarm_id": alarm_id,
                               "unclosed_alarm_count": handover["data"]["unclosed_alarm_count"]})
            if handover["status"] == "completed":
                handover = self._end_outgoing_shift(actor, handover)
        self._resolve_alarm_escalations(actor, alarm_id, "taken_over")
        return takeover

    def takeover_all_handover_alarms(self, actor, handover_id, shift_id):
        handover = self.repository.get_entity(handover_id)
        if not handover or handover["kind"] != "handover":
            raise NotFoundError("handover not found: " + handover_id)
        shift = self.repository.get_entity(shift_id)
        if not shift or shift["kind"] != "shift":
            raise NotFoundError("shift not found: " + shift_id)
        if actor.role not in ("admin", "supervisor") and actor.user_id != shift["data"].get("dispatcher_id"):
            raise PermissionDenied("cannot take over for another dispatcher's shift")
        if handover["data"].get("area") != shift["data"].get("area"):
            raise ValidationError("handover area does not match shift area")
        if handover["status"] != "in_handover":
            raise InvalidTransition("handover is already completed")
        takeover_ids = []
        refreshed = self.repository.refresh_handover(handover_id)
        for alarm_id in refreshed["data"].get("unclosed_alarm_ids", []):
            takeover = self.takeover_alarm(actor, alarm_id, shift_id)
            takeover_ids.append(takeover["id"])
        refreshed = self.repository.refresh_handover(handover_id)
        if refreshed["status"] == "completed":
            refreshed = self._end_outgoing_shift(actor, refreshed)
        return {"handover": refreshed, "takeover_ids": takeover_ids}

    def _end_outgoing_shift(self, actor, handover):
        shift_id = handover["data"]["outgoing_shift_id"]
        shift = self.repository.get_entity(shift_id)
        if not shift or shift["status"] == "ended":
            return self.repository.get_entity(handover["id"])
        updated = self.repository.update_entity(shift_id, shift["version"], "ended", shift["data"])
        self.audit.record(shift_id, actor, "end_shift", shift["status"], "ended",
                          {"handover_id": handover["id"], "reason": "all unclosed alarms taken over"})
        return self.repository.get_entity(handover["id"])

    def end_shift(self, actor, shift_id, expected_version=None):
        shift = self.repository.get_entity(shift_id)
        if not shift or shift["kind"] != "shift":
            raise NotFoundError("shift not found: " + shift_id)
        self._require_shift_actor(actor, shift)
        if shift["status"] == "ended":
            return shift
        handover = self._find_open_handover(shift_id)
        expected = int(expected_version) if expected_version is not None else shift["version"]
        if handover:
            handover = self.repository.refresh_handover(handover["id"])
            pending_ids = handover["data"].get("pending_takeover_alarm_ids", [])
            if pending_ids:
                escalation_ids = self._escalate_pending_alarms(actor, shift, handover, pending_ids)
                raise ConflictError(
                    "%d alarm(s) have no incoming takeover; supervisor escalated" % len(escalation_ids)
                )
            self.repository.update_entity(shift_id, expected, "ended", shift["data"])
            self.audit.record(shift_id, actor, "end_shift", shift["status"], "ended",
                              {"handover_id": handover["id"]})
            return self.repository.get_entity(shift_id)

        pending = []
        for alarm in self._active_area_alarms(shift["data"]["area"]):
            active = self.repository.get_active_takeover(alarm["id"])
            if not active or active["shift_id"] == shift["id"]:
                pending.append(alarm["id"])
        if pending:
            # No handover was opened and no later shift owns the alarm.
            escalation_ids = self._escalate_pending_alarms(
                actor,
                shift,
                None,
                pending,
            )
            raise ConflictError(
                "%d alarm(s) have no next-shift takeover; supervisor escalated" % len(escalation_ids)
            )
        updated = self.repository.update_entity(shift_id, expected, "ended", shift["data"])
        self.audit.record(shift_id, actor, "end_shift", shift["status"], "ended", {"reason": "no open alarms"})
        return updated

    def _resolve_alarm_escalations(self, actor, alarm_id, reason):
        for escalation in self.repository.list_entities(kind="escalation", status="open"):
            if escalation["data"].get("alarm_id") != alarm_id:
                continue
            data = dict(escalation["data"])
            data["resolved_at"] = self._now()
            data["resolution"] = reason
            self.repository.update_entity(
                escalation["id"], escalation["version"], "resolved", data
            )
            self.audit.record(escalation["id"], actor, "resolve_escalation",
                              "open", "resolved", {"alarm_id": alarm_id, "reason": reason})
        return None

    def _escalate_pending_alarms(self, actor, shift, handover, alarm_ids):
        created_ids = []
        for alarm_id in alarm_ids:
            existing = [
                item for item in self.repository.list_entities(kind="escalation")
                if item["status"] == "open"
                and item["data"].get("shift_id") == shift["id"]
                and item["data"].get("alarm_id") == alarm_id
            ]
            if existing:
                created_ids.append(existing[0]["id"])
                continue
            escalation_id = "escalation-%s-%s" % (shift["id"], alarm_id)
            data = {
                "area": shift["data"]["area"],
                "shift_id": shift["id"],
                "dispatcher_id": shift["data"]["dispatcher_id"],
                "alarm_id": alarm_id,
                "handover_id": handover["id"] if handover else None,
                "reason": "alarm_without_next_shift_takeover",
                "notified_role": "supervisor",
                "created_at": self._now(),
            }
            escalation = self.repository.create_entity(
                escalation_id, "escalation", "open", data, actor.user_id
            )
            created_ids.append(escalation["id"])
            self.audit.record(escalation_id, actor, "escalate", None, "open",
                              {"alarm_id": alarm_id, "shift_id": shift["id"]})
        return created_ids

    def list_takeovers(self, alarm_id=None, shift_id=None, status=None):
        return self.repository.list_takeovers(alarm_id=alarm_id, shift_id=shift_id, status=status)

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
