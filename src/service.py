import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
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

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "alarm" and updated["status"] in ("resolved", "closed", "false_alarm"):
            self._on_alarm_closed(updated)
        return updated

    # ------------------------------------------------------------------
    # 值班班次与报警接管
    # ------------------------------------------------------------------

    def _active_shift(self, actor):
        for shift in self.repository.list_entities(kind="shift", status="on_duty"):
            if shift["data"].get("dispatcher_id") == actor.user_id:
                return shift
        return None

    def _active_takeover(self, alarm_id):
        for takeover in self.repository.find_entities("takeover", "alarm_id", alarm_id):
            if takeover["status"] == "active":
                return takeover
        return None

    def _alarm_area(self, alarm):
        equipment = self.repository.get_entity(alarm["data"].get("equipment_id"))
        return equipment["data"].get("location") if equipment else None

    def _unclosed_alarms(self, area):
        result = []
        for alarm in self.repository.list_entities(kind="alarm"):
            if alarm["status"] in ("resolved", "closed", "false_alarm"):
                continue
            if self._alarm_area(alarm) == area:
                result.append(alarm)
        return result

    def _invalidate_takeover(self, takeover, actor=None):
        if takeover["status"] != "active":
            return takeover
        updated = self.repository.update_entity(
            takeover["id"], takeover["version"], "invalid", dict(takeover["data"])
        )
        self.audit.record(
            takeover["id"], actor or Actor("system", "admin"), "invalidate", "active", "invalid",
            {"alarm_id": takeover["data"].get("alarm_id")},
        )
        return updated

    def _recalculate_handovers(self, area):
        count = len(self._unclosed_alarms(area))
        for handover in self.repository.list_entities(kind="handover", status="pending"):
            if handover["data"].get("area") != area:
                continue
            if handover["data"].get("unclosed_count") == count:
                continue
            merged = dict(handover["data"])
            merged["unclosed_count"] = count
            self.repository.update_entity(handover["id"], handover["version"], "pending", merged)
        return count

    def _on_alarm_closed(self, alarm):
        takeover = self._active_takeover(alarm["id"])
        if takeover:
            self._invalidate_takeover(takeover)
        area = self._alarm_area(alarm)
        if area:
            self._recalculate_handovers(area)

    def start_shift(self, actor, data):
        payload = dict(data or {})
        payload["dispatcher_id"] = actor.user_id
        payload["started_at"] = utcnow()
        self.rules.validate_create(actor, "shift", payload, self._lookup)
        shift_id = str(payload.pop("id", "") or uuid4())
        entity = self.repository.create_entity(shift_id, "shift", "on_duty", payload, actor.user_id)
        self.audit.record(shift_id, actor, "start_shift", None, "on_duty", {"area": payload.get("area")})
        return entity

    def take_over_alarm(self, actor, alarm_id):
        shift = self._active_shift(actor)
        if not shift:
            raise ValidationError("dispatcher is not on duty")
        alarm = self.repository.get_entity(alarm_id)
        if not alarm:
            raise NotFoundError("alarm not found: " + alarm_id)
        if alarm["status"] in ("resolved", "closed", "false_alarm"):
            raise ValidationError("cannot take over a resolved, closed or false alarm")
        area = self._alarm_area(alarm)
        if area != shift["data"].get("area"):
            raise ValidationError("alarm is not in your shift area")
        existing = self._active_takeover(alarm_id)
        transfer = False
        if existing:
            if existing["data"].get("shift_id") == shift["id"]:
                return existing
            handover = self._find_handover(existing["data"].get("shift_id"), shift["id"])
            if not handover or handover["status"] != "pending":
                raise ConflictError("alarm already taken over by another dispatcher")
            transfer = True
        takeover_id = str(uuid4())
        entity = self.repository.takeover_alarm(
            alarm_id, takeover_id, shift["id"], actor.user_id, area, actor.user_id, transfer=transfer
        )
        self.audit.record(
            takeover_id, actor, "take_over", None, "active",
            {"alarm_id": alarm_id, "shift_id": shift["id"]},
        )
        self._maybe_complete_handover(shift)
        return entity

    def take_over_all(self, actor):
        shift = self._active_shift(actor)
        if not shift:
            raise ValidationError("dispatcher is not on duty")
        taken = []
        for alarm in self._unclosed_alarms(shift["data"].get("area")):
            existing = self._active_takeover(alarm["id"])
            if existing and existing["data"].get("shift_id") == shift["id"]:
                taken.append(existing)
                continue
            taken.append(self.take_over_alarm(actor, alarm["id"]))
        self._maybe_complete_handover(shift)
        return taken

    def _find_handover(self, from_shift_id, to_shift_id):
        for handover in self.repository.list_entities(kind="handover"):
            if (
                handover["data"].get("from_shift_id") == from_shift_id
                and handover["data"].get("to_shift_id") == to_shift_id
            ):
                return handover
        return None

    def _maybe_complete_handover(self, to_shift):
        for handover in self.repository.list_entities(kind="handover", status="pending"):
            if handover["data"].get("to_shift_id") != to_shift["id"]:
                continue
            if self._all_taken_over(handover):
                self._complete_handover_record(handover)

    def _all_taken_over(self, handover):
        to_shift = self.repository.get_entity(handover["data"].get("to_shift_id"))
        if not to_shift:
            return False
        for alarm in self._unclosed_alarms(handover["data"].get("area")):
            takeover = self._active_takeover(alarm["id"])
            if not takeover or takeover["data"].get("shift_id") != to_shift["id"]:
                return False
        return True

    def initiate_handover(self, actor, from_shift_id, to_shift_id):
        payload = {"from_shift_id": from_shift_id, "to_shift_id": to_shift_id}
        self.rules.validate_create(actor, "handover", payload, self._lookup)
        from_shift = self.repository.get_entity(from_shift_id)
        area = from_shift["data"].get("area")
        payload["area"] = area
        payload["unclosed_count"] = len(self._unclosed_alarms(area))
        handover_id = str(uuid4())
        entity = self.repository.create_entity(handover_id, "handover", "pending", payload, actor.user_id)
        self.audit.record(
            handover_id, actor, "initiate_handover", None, "pending",
            {"from_shift_id": from_shift_id, "to_shift_id": to_shift_id, "unclosed_count": payload["unclosed_count"]},
        )
        return entity

    def _complete_handover_record(self, handover):
        to_shift = self.repository.get_entity(handover["data"].get("to_shift_id"))
        from_shift = self.repository.get_entity(handover["data"].get("from_shift_id"))
        updated = self.repository.update_entity(
            handover["id"], handover["version"], "completed", dict(handover["data"])
        )
        self.audit.record(
            handover["id"], Actor("system", "admin"), "complete_handover", "pending", "completed",
            {"from_shift_id": from_shift["id"], "to_shift_id": to_shift["id"]},
        )
        if from_shift["status"] in ("on_duty", "handing_over"):
            self.repository.update_entity(
                from_shift["id"], from_shift["version"], "ended", dict(from_shift["data"])
            )
            self.audit.record(
                from_shift["id"], Actor("system", "admin"), "end_shift", from_shift["status"], "ended",
                {"handover_id": handover["id"]},
            )
        return updated

    def complete_handover(self, actor, handover_id):
        handover = self.repository.get_entity(handover_id)
        if not handover:
            raise NotFoundError("handover not found: " + handover_id)
        if handover["status"] != "pending":
            raise ValidationError("handover is not pending")
        if not self._all_taken_over(handover):
            raise ValidationError("cannot complete handover: not all unclosed alarms taken over")
        return self._complete_handover_record(handover)

    def end_shift(self, actor, shift_id):
        shift = self.repository.get_entity(shift_id)
        if not shift:
            raise NotFoundError("shift not found: " + shift_id)
        if shift["status"] not in ("on_duty", "handing_over"):
            raise ValidationError("shift is not active")
        next_status, patch = self.rules.validate_transition(actor, shift, "end", {}, self._lookup)
        merged = dict(shift["data"])
        merged.update(patch)
        updated = self.repository.update_entity(shift_id, shift["version"], next_status, merged)
        self.audit.record(shift_id, actor, "end", shift["status"], next_status, {})
        self._escalate_untaken(updated)
        return updated

    def _escalate_untaken(self, shift):
        area = shift["data"].get("area")
        for alarm in self._unclosed_alarms(area):
            takeover = self._active_takeover(alarm["id"])
            if takeover:
                continue
            payload = {
                "shift_id": shift["id"],
                "alarm_id": alarm["id"],
                "area": area,
                "reason": "alarm not taken over before shift end",
                "escalated_to": "supervisor",
                "escalated_at": utcnow(),
            }
            escalation_id = str(uuid4())
            self.repository.create_entity(escalation_id, "escalation", "pending", payload, "system")
            self.audit.record(
                escalation_id, Actor("system", "admin"), "escalate", None, "pending",
                {"shift_id": shift["id"], "alarm_id": alarm["id"]},
            )

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
