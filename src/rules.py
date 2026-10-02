from datetime import datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] == "closed":
        raise ValidationError("rescue_job requires an active alarm")
    key = data.get("dedupe_key")
    for job in _all(lookup, "rescue_job"):
        if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
            raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _validate_shift(data, lookup):
    area = str(data.get("area", "")).strip()
    if not area:
        raise ValidationError("area is required")
    dispatcher_id = data.get("dispatcher_id")
    if dispatcher_id:
        for shift in _all(lookup, "shift"):
            if (
                shift["status"] == "on_duty"
                and shift["data"].get("dispatcher_id") == dispatcher_id
                and shift["data"].get("area") == area
            ):
                raise ConflictError("dispatcher already on duty in this area")


def _validate_takeover(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm:
        raise ValidationError("takeover requires alarm")
    if alarm["status"] in ("resolved", "closed", "false_alarm"):
        raise ValidationError("cannot take over a resolved, closed or false alarm")
    shift = _find_one(lookup, "shift", "id", data.get("shift_id"))
    if not shift or shift["status"] != "on_duty":
        raise ValidationError("takeover requires an on-duty shift")
    equipment = _find_one(lookup, "equipment", "id", alarm["data"].get("equipment_id"))
    if equipment and equipment["data"].get("location") != shift["data"].get("area"):
        raise ValidationError("alarm is not in your shift area")


def _validate_handover(data, lookup):
    from_shift = _find_one(lookup, "shift", "id", data.get("from_shift_id"))
    to_shift = _find_one(lookup, "shift", "id", data.get("to_shift_id"))
    if not from_shift or not to_shift:
        raise ValidationError("handover requires both shifts")
    if from_shift["status"] != "on_duty":
        raise ValidationError("from_shift must be on duty to hand over")
    if to_shift["status"] != "on_duty":
        raise ValidationError("to_shift must be on duty to take over")
    if from_shift["data"].get("area") != to_shift["data"].get("area"):
        raise ValidationError("shifts must be in the same area")
    if from_shift["data"].get("dispatcher_id") == to_shift["data"].get("dispatcher_id"):
        raise ValidationError("cannot hand over to the same dispatcher")


def _validate_escalation(data, lookup):
    if not _find_one(lookup, "shift", "id", data.get("shift_id")):
        raise ValidationError("escalation requires shift")
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm:
        raise ValidationError("escalation requires alarm")
    if alarm["status"] in ("closed", "false_alarm"):
        raise ValidationError("escalation only applies to unclosed alarms")


def _grant_permit(actor, entity, data, lookup):
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if not equipment or equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    inspections = [i for i in _all(lookup, "inspection") if i["data"].get("equipment_id") == equipment["id"] and i["status"] == "passed"]
    if not inspections:
        raise ConflictError("permit requires a passed inspection")
    if [r for r in _all(lookup, "remediation") if r["data"].get("equipment_id") == equipment["id"] and r["status"] != "closed"]:
        raise ConflictError("permit blocked by open remediation")
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "shifts": "shift", "takeovers": "takeover",
        "handovers": "handover", "escalations": "escalation",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "dispatched", "remediation": "open",
        "permit": "blocked", "shift": "on_duty", "takeover": "active",
        "handover": "pending", "escalation": "pending",
    }
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended",), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("dispatched", "on_site"), "aborted"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
        "shift": {
            "start_handover": (("on_duty",), "handing_over"),
            "end": (("on_duty", "handing_over"), "ended"),
        },
        "takeover": {
            "invalidate": (("active",), "invalid"),
        },
        "handover": {
            "complete": (("pending",), "completed"),
        },
        "escalation": {
            "notify": (("pending",), "notified"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id", "dedupe_key", "team"),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
        "shift": ("area",),
        "takeover": ("alarm_id",),
        "handover": ("from_shift_id", "to_shift_id"),
        "escalation": ("shift_id", "alarm_id", "reason"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
        "shift": ("admin", "dispatcher"),
        "takeover": ("admin", "dispatcher"),
        "handover": ("admin", "dispatcher"),
        "escalation": ("admin", "dispatcher"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "arrive": ("admin", "dispatcher"),
        "abort": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        "expire": ("admin", "inspector"),
        "start_handover": ("admin", "dispatcher"),
        "end": ("admin", "dispatcher"),
        "invalidate": ("admin", "dispatcher"),
        "notify": ("admin", "dispatcher"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
        "shift": lambda a, d, l: _validate_shift(d, l),
        "takeover": lambda a, d, l: _validate_takeover(d, l),
        "handover": lambda a, d, l: _validate_handover(d, l),
        "escalation": lambda a, d, l: _validate_escalation(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
