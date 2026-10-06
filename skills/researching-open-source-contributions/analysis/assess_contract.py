"""The field types of a candidate assessment, in one place (#266, defect 1).

The candidate verifier's ``record`` gate (verify_candidates.py ``_assessment_shape_violations`` and
``_assessment_gate_violations``) checks the type of every assessment field it records. Its checks are code, not a
table, so this module holds them as one: the assess prompt states each field's type from it (stage_inputs.py
assess-inputs) and the strict check (verify_e2e_result.py R3) checks each field against it. The verifier is read,
never edited: its key lists (READINESS_KEYS, POLICY_CHECK_KEYS) come from the module, and test_assess_contract.py
runs the verifier's own checks over every field with a value of every JSON type and requires the same verdict as
this table, so the two cannot drift apart unnoticed.

Value rules beyond the type (status and reason pairs, readiness results, ready conditions, link scope, policy
bytes) stay where they are: the prompt's rules, R5, A2 and the record gate itself (A3).
"""

from __future__ import annotations

import re

STRING, STRINGS, STRINGS_OR_NULL, STRING_OR_NULL = "string", "string array", "string array or null", "string or null"
BOOLEAN_OR_NULL, INTEGER, OBJECT, OBJECT_OR_NULL = "boolean or null", "integer", "object", "object or null"
NULL = "null"

KIND_KO = {STRING: "문자열", STRINGS: "문자열 배열", STRINGS_OR_NULL: "문자열 배열 또는 null",
           STRING_OR_NULL: "문자열 또는 null", BOOLEAN_OR_NULL: "true, false 또는 null", INTEGER: "정수",
           OBJECT: "객체", OBJECT_OR_NULL: "객체 또는 null", NULL: "null"}

CANDIDATE_REF = r"CAN-\d{3,}"
SHA256_HEX = r"[0-9a-f]{64}"
# A value of the pattern, for the sync test's representatives.
PATTERN_SAMPLES = {CANDIDATE_REF: "CAN-001", SHA256_HEX: "0" * 64}

# Per field, what the prompt adds to the type (the method rules of reproduction.public_steps above all: the
# 2026-10-05 second assess answer wrote it as one string).
NOTES = {
    "reproduction.public_steps": ('`method` 가 `none` 이면 `null`. `static` 이면 공개로 따라 할 수 있는 단계 문장의 '
                                  '배열(예: `"public_steps": ["head 커밋의 docs/rules/x.md 를 연다", '
                                  '"## 헤딩 목록에 Options 가 없다"]`) 또는 `null`. 문장 하나라도 문자열 하나로 쓰지 말고 '
                                  '배열에 넣는다'),
    "reproduction.method": "`static`, `executed`, `none` 중 하나(이 판정은 `static` 또는 `none`)",
    "superseded_by": "`null`(또는 `CAN-<번호>`)",
    "policy_checks.<키>.sha256": "`null` 또는 64자리 소문자 hex",
    "execution_evidence": "이 판정은 아무것도 실행하지 않으므로 `null`",
    "policy_checks.<키>.found": "`program_rules` 는 `null` 만(이 판정에는 프로그램 규칙 입력이 없다)",
    "verified_at": "쓰지 않는다(호스트가 채운다)",
    "verified_base_sha": "쓰지 않는다(호스트가 채운다)",
}

# Values of a field's own type that the record gate still refuses for another field's sake (not a type rule; the
# prompt states each). The sync test leaves exactly these out.
CROSS_FIELD = {
    ("execution_evidence", "null"): "reproduction.method executed needs execution evidence",
}

TOP_LEVEL = [
    ("pattern_id", STRING), ("repository", STRING), ("locus", STRING), ("status", STRING),
    ("status_reason", STRING), ("sensitivity", STRING), ("summary", STRING), ("impact", STRING),
    ("readiness_checks", OBJECT), ("duplicate_verdict", OBJECT), ("ai_policy_status", STRING),
    ("disclosure_required", BOOLEAN_OR_NULL), ("private_evidence_reference", STRING_OR_NULL),
    ("evidence_links", STRINGS), ("execution_evidence", OBJECT_OR_NULL), ("reproduction", OBJECT),
    ("policy_checks", OBJECT), ("blocking_gaps", STRINGS), ("superseded_by", STRING_OR_NULL),
]
OPTIONAL_TOP_LEVEL = [("verified_at", STRING), ("verified_base_sha", STRING)]
PATTERNS = {"superseded_by": CANDIDATE_REF}


def field_types(vc) -> list:
    """[(path, kind, pattern or None)]: every assessment field the verifier checks, parents before children."""
    rows = [(path, kind, PATTERNS.get(path)) for path, kind in TOP_LEVEL + OPTIONAL_TOP_LEVEL]
    for key in vc.READINESS_KEYS:
        base = "readiness_checks." + key
        rows += [(base, OBJECT, None), (base + ".result", STRING, None), (base + ".evidence_links", STRINGS, None),
                 (base + ".note", STRING, None)]
    rows += [("duplicate_verdict.matched_items", STRINGS, None), ("duplicate_verdict.judgment", STRING, None)]
    rows += [("execution_evidence.approval", OBJECT, None), ("execution_evidence.approval.granted_by", STRING, None),
             ("execution_evidence.approval.granted_at", STRING, None),
             ("execution_evidence.approval.scope", STRING, None)]
    rows += [("execution_evidence." + name, STRING, None)
             for name in ("command", "isolation", "runtime", "network_policy", "output_location")]
    rows += [("execution_evidence.exit_code", INTEGER, None), ("execution_evidence.output_excerpt", STRING_OR_NULL, None)]
    rows += [("reproduction.method", STRING, None), ("reproduction.evidence_links", STRINGS, None),
             ("reproduction.public_steps", STRINGS_OR_NULL, None)]
    for key in vc.POLICY_CHECK_KEYS:
        base = "policy_checks." + key
        # without a program rules input (assess-inputs has none) the record gate takes only null there
        found = NULL if key == "program_rules" else BOOLEAN_OR_NULL
        rows += [(base, OBJECT, None), (base + ".found", found, None), (base + ".source", STRING_OR_NULL, None),
                 (base + ".sha256", STRING_OR_NULL, SHA256_HEX), (base + ".assessment", STRING, None),
                 (base + ".evidence_links", STRINGS, None)]
    return rows


def closed_objects(vc) -> list:
    """The objects whose keys are exactly their rows' children (the verifier refuses a missing or an extra key)."""
    paths = [path for path, _, _ in field_types(vc)]
    return [path for path, kind, _ in field_types(vc)
            if kind in (OBJECT, OBJECT_OR_NULL) and any(other.startswith(path + ".") for other in paths)]


def kind_of(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "string array" if all(isinstance(item, str) for item in value) else "array"
    return "object" if isinstance(value, dict) else type(value).__name__


def type_ok(kind: str, value) -> bool:
    found = kind_of(value)
    allowed = {STRING: ("string",), STRINGS: ("string array",), STRINGS_OR_NULL: ("string array", "null"),
               STRING_OR_NULL: ("string", "null"), BOOLEAN_OR_NULL: ("boolean", "null"), INTEGER: ("integer",),
               OBJECT: ("object",), OBJECT_OR_NULL: ("object", "null"), NULL: ("null",)}[kind]
    return found in allowed


def type_problems(item, location: str, vc) -> list:
    """Every field of one assessment that is not of its type (or an object without exactly its keys), as
    ``<location>.<path>: expected <kind> (got <kind>)``. A field under a parent of the wrong type is not looked at
    (the parent is named); a missing or extra top-level field is the verifier's shape check's (R3 runs it too)."""
    if not isinstance(item, dict):
        return []
    rows = field_types(vc)
    children = {}
    for path, _, _ in rows:
        if "." in path:
            children.setdefault(path.rsplit(".", 1)[0], []).append(path.rsplit(".", 1)[1])
    problems, bad = [], set()
    for path, kind, pattern in rows:
        parent = path.rsplit(".", 1)[0] if "." in path else None
        if parent is not None and (parent in bad or any(parent.startswith(b + ".") for b in bad)):
            continue
        container = item
        if parent is not None:
            for part in parent.split("."):
                container = container.get(part) if isinstance(container, dict) else None
            if container is None:          # an object-or-null parent that is null holds nothing
                continue
        name = path.rsplit(".", 1)[-1]
        if name not in container:
            if parent is not None:
                problems.append("%s.%s: missing" % (location, path))
                bad.add(path)
            continue
        value = container[name]
        if not type_ok(kind, value):
            problems.append("%s.%s: expected %s (got %s)" % (location, path, kind, kind_of(value)))
            bad.add(path)
            continue
        if pattern is not None and isinstance(value, str) and re.fullmatch(pattern, value) is None:
            problems.append("%s.%s: expected %s matching %s" % (location, path, kind, pattern))
            bad.add(path)
            continue
        if isinstance(value, dict) and path in children:
            extra = sorted(set(value) - set(children[path]))
            if extra:
                problems.append("%s.%s: unsupported key %s" % (location, path, ", ".join(extra)))
    return problems


def prompt_lines(vc) -> str:
    """The field type section of the assess prompt: one line per field, readiness and policy keys folded."""
    lines, seen = [], set()
    for path, kind, _ in field_types(vc):
        parts = path.split(".")
        if parts[0] in ("readiness_checks", "policy_checks") and len(parts) > 1:
            parts[1] = "<키>"
        shown = ".".join(parts)
        if shown in seen:
            continue
        seen.add(shown)
        note = NOTES.get(shown)
        lines.append("- `%s`: %s%s" % (shown, KIND_KO[kind], " — " + note if note else ""))
    return "\n".join(lines)
