"""
CrisisDesk Agent Prompt Builders
Each agent's system prompt and response parser in one place.
"""
import json
from backend.simulation.world import rulebook_text, severity_tier, travel_time_seconds, INCIDENT_TYPES

# ─── Triage Agent ──────────────────────────────────────────────────────────────

TRIAGE_SYSTEM = """You are the TRIAGE agent in CrisisDesk, a simulated emergency dispatch system.
Your ONLY job: assess how severe and urgent an incident is. You do NOT know what resources are available.

""" + rulebook_text() + """

You will be given this incident type's DEPARTMENT BASELINE SEVERITY — a historical
reference point for how serious this category of incident typically is. Anchor your
score near that baseline, and adjust it up or down only based on specifics in the
description (e.g. confirmed injuries, entrapment, spreading hazard) — don't ignore
the baseline and invent a score from scratch, and don't just copy it verbatim either.
Two incidents of genuinely different types and different baselines should essentially
never end up with the exact same severity score unless the description gives a
specific reason for that.

Respond ONLY with valid JSON:
{"severity_score": <float 0-10>, "priority_tier": "CRITICAL"|"URGENT"|"LOW", "reasoning": "<one or two sentences>"}"""


def build_triage_prompt(incident: dict) -> list:
    baseline = INCIDENT_TYPES.get(incident["incident_type"], {}).get("base_severity", "unknown")
    return [
        {"role": "system", "content": TRIAGE_SYSTEM},
        {"role": "user", "content": f"""INCIDENT:
Type: {incident['incident_type']}
Description: {incident['description']}
Location: ({incident['x']}, {incident['y']})
Department baseline severity for this incident type: {baseline}/10

Assess this incident."""}
    ]


def parse_triage(text: str, fallback: float = 5.0) -> dict:
    try:
        data = _parse_json(text)
        score = max(0.0, min(10.0, float(data.get("severity_score", fallback))))
        return {"severity_score": score, "priority_tier": data.get("priority_tier") or severity_tier(score), "reasoning": data.get("reasoning", "")}
    except Exception:
        return {"severity_score": fallback, "priority_tier": severity_tier(fallback), "reasoning": "Fallback."}


# ─── Allocator Agent ───────────────────────────────────────────────────────────

ALLOCATOR_SYSTEM = """You are the RESOURCE ALLOCATOR agent in CrisisDesk.
Your job: given a triaged incident and the list of AVAILABLE resources, propose which resource should be dispatched.

""" + rulebook_text() + """

Respond ONLY with valid JSON:
{"recommended_resource_id": "<id or null>", "reasoning": "<explanation>", "alternative_resource_id": "<id or null>"}"""


def build_allocator_prompt(incident: dict, available: list, pending_critical_count: int = 0) -> list:
    lines = "\n".join(
        f"  - {r['resource_id']} ({r['resource_type']}) at ({r['x']}, {r['y']}) — ETA {travel_time_seconds(incident['x'], incident['y'], r['x'], r['y'])}s"
        for r in available
    ) or "  (none available)"
    return [
        {"role": "system", "content": ALLOCATOR_SYSTEM},
        {"role": "user", "content": f"""INCIDENT:
Type: {incident['incident_type']}  Severity: {incident.get('severity_score','?')}  Tier: {incident.get('priority_tier','?')}
Location: ({incident['x']}, {incident['y']})  Preferred resource type: {incident.get('preferred_resource','?')}

AVAILABLE RESOURCES:
{lines}

OTHER UNRESOLVED CRITICAL INCIDENTS: {pending_critical_count}

Propose the best assignment."""}
    ]


def parse_allocator(text: str) -> dict:
    try:
        data = _parse_json(text)
        return {"recommended_resource_id": data.get("recommended_resource_id"), "reasoning": data.get("reasoning", ""), "alternative_resource_id": data.get("alternative_resource_id")}
    except Exception:
        return {"recommended_resource_id": None, "reasoning": "Fallback.", "alternative_resource_id": None}


# ─── Auditor Agent ─────────────────────────────────────────────────────────────

AUDITOR_SYSTEM = """You are the AUDITOR agent in CrisisDesk.
Your ONLY job: review a proposed resource assignment against the hard rules below. If you reject, cite the exact rule violated.

""" + rulebook_text() + """

CRITICAL: the "Proposed resource already assigned elsewhere?" flag in the prompt is
ground truth read directly from the dispatch database at this exact moment — it is
not something to re-derive or second-guess. If it says False, the resource is NOT
double-assigned, full stop, regardless of what other incidents exist elsewhere in
the system. The "Other unresolved CRITICAL incidents" list is separate, purely
informational context about system-wide load — it does NOT mean the proposed
resource is tied up in any of those incidents. Never reject citing HARD RULE 1
(double-assignment) unless the flag explicitly says True.

Respond ONLY with valid JSON:
{"approved": true|false, "violation_type": "<rule violated or null>", "explanation": "<clear explanation>"}"""


def build_auditor_prompt(incident: dict, proposal: dict, context: dict) -> list:
    pending = "\n".join(f"  - {p}" for p in context.get("pending_critical_incidents", [])) or "  (none)"
    already_assigned = context.get("resource_already_assigned", False)
    return [
        {"role": "system", "content": AUDITOR_SYSTEM},
        {"role": "user", "content": f"""INCIDENT: {incident['incident_type']} — Tier: {incident.get('priority_tier','?')} — Severity: {incident.get('severity_score','?')}

ALLOCATOR'S PROPOSAL:
Resource: {proposal.get('recommended_resource_id')}
Reasoning: {proposal.get('reasoning')}

CONTEXT:
Proposed resource already assigned elsewhere (GROUND TRUTH — trust this exactly, do not infer otherwise)? {already_assigned}

Other unresolved CRITICAL incidents (informational only — NOT evidence the proposed resource is assigned to any of them):
{pending}

Approve or reject."""}
    ]


def parse_auditor(text: str) -> dict:
    try:
        data = _parse_json(text)
        return {"approved": bool(data.get("approved", False)), "violation_type": data.get("violation_type"), "explanation": data.get("explanation", "")}
    except Exception:
        return {"approved": True, "violation_type": "PARSE_ERROR", "explanation": "Fallback — defaulted to approve."}


# ─── Liaison Agent ─────────────────────────────────────────────────────────────

LIAISON_SYSTEM = """You are the FIELD LIAISON agent in CrisisDesk.
Your job: take an APPROVED resource assignment and write clear, specific dispatch instructions (3 sentences max).

Respond ONLY with valid JSON:
{"instructions": "<dispatch message>", "eta_seconds": <number>}"""


def build_liaison_prompt(incident: dict, resource: dict) -> tuple:
    eta = travel_time_seconds(incident["x"], incident["y"], resource["x"], resource["y"])
    return [
        {"role": "system", "content": LIAISON_SYSTEM},
        {"role": "user", "content": f"""APPROVED ASSIGNMENT:
Resource: {resource['resource_id']} ({resource['resource_type']}) at ({resource['x']}, {resource['y']})
Incident: {incident['incident_type']} — {incident['description']} at ({incident['x']}, {incident['y']})
Tier: {incident.get('priority_tier','?')}  ETA: {eta}s

Write the dispatch instructions."""}
    ], eta


def parse_liaison(text: str, fallback_eta: float) -> dict:
    try:
        data = _parse_json(text)
        return {"instructions": data.get("instructions", "Proceed to incident."), "eta_seconds": float(data.get("eta_seconds", fallback_eta))}
    except Exception:
        return {"instructions": "Proceed to incident immediately.", "eta_seconds": fallback_eta}


# ─── Single-Agent Baseline ─────────────────────────────────────────────────────

SINGLE_AGENT_SYSTEM = """You are an autonomous emergency dispatch AI (single-agent baseline for CrisisDesk).
You handle the ENTIRE decision process alone: severity assessment, resource selection, and dispatch instructions.

""" + rulebook_text() + """

Respond ONLY with valid JSON:
{"severity_score": <float>, "priority_tier": "CRITICAL"|"URGENT"|"LOW", "recommended_resource_id": "<id or null>", "instructions": "<dispatch text>", "reasoning": "<explanation>"}"""


def build_single_agent_prompt(incident: dict, available: list, pending_critical: list) -> list:
    baseline = INCIDENT_TYPES.get(incident["incident_type"], {}).get("base_severity", "unknown")
    lines = "\n".join(
        f"  - {r['resource_id']} ({r['resource_type']}) at ({r['x']}, {r['y']}) — ETA {travel_time_seconds(incident['x'], incident['y'], r['x'], r['y'])}s"
        for r in available
    ) or "  (none available)"
    pending_block = "\n".join(f"  - {p}" for p in pending_critical) or "  (none)"
    return [
        {"role": "system", "content": SINGLE_AGENT_SYSTEM},
        {"role": "user", "content": f"""INCIDENT:
Type: {incident['incident_type']}  Location: ({incident['x']}, {incident['y']})
Department baseline severity for this incident type: {baseline}/10

AVAILABLE RESOURCES:
{lines}

OTHER UNRESOLVED CRITICAL INCIDENTS:
{pending_block}

Make the full decision."""}
    ]


def parse_single_agent(text: str) -> dict:
    try:
        data = _parse_json(text)
        return {
            "severity_score": max(0.0, min(10.0, float(data.get("severity_score", 5.0)))),
            "priority_tier": data.get("priority_tier", "URGENT"),
            "recommended_resource_id": data.get("recommended_resource_id"),
            "instructions": data.get("instructions", ""),
            "reasoning": data.get("reasoning", ""),
        }
    except Exception:
        return {"severity_score": 5.0, "priority_tier": "URGENT", "recommended_resource_id": None, "instructions": "", "reasoning": "Fallback."}


# ─── Shared helper ─────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    return json.loads(cleaned.strip())
