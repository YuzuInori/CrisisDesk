"""
CrisisDesk Database Layer
Fixed: escalated_no_resource incidents never get resolved_at set.
Fixed: resource UNIQUE constraint is per-run, not global.
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "crisisdesk.db"


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            incident_type TEXT NOT NULL,
            description TEXT NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL,
            reported_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            severity_score REAL,
            priority_tier TEXT,
            resolved_at TEXT,
            response_time_seconds REAL,
            UNIQUE(incident_id, run_id)
        );

        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            x REAL NOT NULL,
            y REAL NOT NULL,
            status TEXT DEFAULT 'available',
            assigned_incident_id TEXT,
            busy_until TEXT,
            UNIQUE(resource_id, run_id)
        );

        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            incident_id TEXT NOT NULL,
            agent_role TEXT NOT NULL,
            message_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            incident_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            description TEXT NOT NULL,
            proposed_by TEXT NOT NULL,
            flagged_by TEXT NOT NULL,
            resolution TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            incident_id TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            instructions TEXT NOT NULL,
            eta_seconds REAL,
            dispatched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS benchmark_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            mode TEXT NOT NULL,
            scenario_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            total_incidents INTEGER DEFAULT 0,
            resolved_incidents INTEGER DEFAULT 0,
            escalated_incidents INTEGER DEFAULT 0,
            priority_violations INTEGER DEFAULT 0,
            double_assignments INTEGER DEFAULT 0,
            avg_response_time_seconds REAL,
            quality_score REAL,
            total_decision_time_ms REAL,
            raw_log TEXT
        );
    """)
    conn.commit()
    conn.close()


def insert_incident(incident_id, run_id, incident_type, description, x, y):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.execute("""
        INSERT OR IGNORE INTO incidents
        (incident_id, run_id, incident_type, description, x, y, reported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (incident_id, run_id, incident_type, description, x, y, now))
    conn.commit()
    if cur.rowcount == 0:
        print(f"[WARNING] insert_incident: collision for {incident_id!r} in run {run_id!r}")
    conn.close()


def update_incident_triage(incident_id, severity_score, priority_tier):
    conn = get_conn()
    conn.execute("""
        UPDATE incidents SET severity_score=?, priority_tier=?, status='triaged'
        WHERE incident_id=?
    """, (severity_score, priority_tier, incident_id))
    conn.commit()
    conn.close()


def resolve_incident(incident_id):
    """Mark incident as resolved with response time. Only called when truly dispatched."""
    conn = get_conn()
    row = conn.execute("SELECT reported_at FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
    now = datetime.utcnow()
    response_time = None
    if row:
        try:
            reported = datetime.fromisoformat(row["reported_at"])
            response_time = (now - reported).total_seconds()
        except Exception:
            pass
    conn.execute("""
        UPDATE incidents SET status='resolved', resolved_at=?, response_time_seconds=?
        WHERE incident_id=?
    """, (now.isoformat(), response_time, incident_id))
    conn.commit()
    conn.close()


def escalate_incident(incident_id):
    """Mark incident as escalated. Does NOT set resolved_at — these are separate statuses."""
    conn = get_conn()
    conn.execute("""
        UPDATE incidents SET status='escalated_no_resource'
        WHERE incident_id=?
    """, (incident_id,))
    conn.commit()
    conn.close()


def update_incident_status(incident_id, status):
    conn = get_conn()
    conn.execute("UPDATE incidents SET status=? WHERE incident_id=?", (status, incident_id))
    conn.commit()
    conn.close()


def log_agent_message(run_id, incident_id, agent_role, message_type, content):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO agent_messages (run_id, incident_id, agent_role, message_type, content, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (run_id, incident_id, agent_role, message_type,
          json.dumps(content) if not isinstance(content, str) else content, now))
    conn.commit()
    conn.close()


def log_conflict(run_id, incident_id, conflict_type, description, proposed_by, flagged_by):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO conflicts (run_id, incident_id, conflict_type, description, proposed_by, flagged_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (run_id, incident_id, conflict_type, description, proposed_by, flagged_by, now))
    conn.commit()
    conflict_id = cur.lastrowid
    conn.close()
    return conflict_id


def resolve_conflict(conflict_id, resolution):
    conn = get_conn()
    conn.execute("UPDATE conflicts SET resolution=?, resolved=1 WHERE id=?", (resolution, conflict_id))
    conn.commit()
    conn.close()


def log_dispatch(run_id, incident_id, resource_id, instructions, eta_seconds):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT INTO dispatches (run_id, incident_id, resource_id, instructions, eta_seconds, dispatched_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (run_id, incident_id, resource_id, instructions, eta_seconds, now))
    conn.commit()
    conn.close()


def insert_resource(resource_id, run_id, resource_type, x, y):
    conn = get_conn()
    cur = conn.execute("""
        INSERT OR IGNORE INTO resources (resource_id, run_id, resource_type, x, y)
        VALUES (?, ?, ?, ?, ?)
    """, (resource_id, run_id, resource_type, x, y))
    conn.commit()
    if cur.rowcount == 0:
        print(f"[WARNING] insert_resource: collision for {resource_id!r} in run {run_id!r}")
    conn.close()


def assign_resource(resource_id, incident_id, busy_until_iso):
    conn = get_conn()
    conn.execute("""
        UPDATE resources SET status='dispatched', assigned_incident_id=?, busy_until=?
        WHERE resource_id=?
    """, (incident_id, busy_until_iso, resource_id))
    conn.commit()
    conn.close()


def get_resources_by_run(run_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM resources WHERE run_id=? ORDER BY resource_id", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_available_resources(run_id, resource_type=None):
    conn = get_conn()
    if resource_type:
        rows = conn.execute("""
            SELECT * FROM resources WHERE run_id=? AND status='available' AND resource_type=?
        """, (run_id, resource_type)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM resources WHERE run_id=? AND status='available'
        """, (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_incidents_by_run(run_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM incidents WHERE run_id=? ORDER BY reported_at", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_incident(incident_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_messages_by_run(run_id, incident_id=None):
    conn = get_conn()
    if incident_id:
        rows = conn.execute("""
            SELECT * FROM agent_messages WHERE run_id=? AND incident_id=? ORDER BY id
        """, (run_id, incident_id)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM agent_messages WHERE run_id=? ORDER BY id
        """, (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_conflicts_by_run(run_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM conflicts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_dispatches_by_run(run_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM dispatches WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_benchmark_run(run_id, mode, scenario_name):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO benchmark_runs (run_id, mode, scenario_name, started_at)
        VALUES (?, ?, ?, ?)
    """, (run_id, mode, scenario_name, now))
    conn.commit()
    conn.close()


def complete_benchmark_run(run_id, total_incidents, resolved_incidents, escalated_incidents,
                            priority_violations, double_assignments, avg_response_time_seconds,
                            quality_score, total_decision_time_ms, raw_log=None):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("""
        UPDATE benchmark_runs SET
            completed_at=?, total_incidents=?, resolved_incidents=?, escalated_incidents=?,
            priority_violations=?, double_assignments=?, avg_response_time_seconds=?,
            quality_score=?, total_decision_time_ms=?, raw_log=?
        WHERE run_id=?
    """, (now, total_incidents, resolved_incidents, escalated_incidents,
          priority_violations, double_assignments, avg_response_time_seconds,
          quality_score, total_decision_time_ms, json.dumps(raw_log) if raw_log else None, run_id))
    conn.commit()
    conn.close()


def get_benchmark_run(run_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM benchmark_runs WHERE run_id=?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_benchmark_runs():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM benchmark_runs ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_run(run_id):
    """Permanently removes every row tied to run_id — incidents, resources,
    agent messages, conflicts, dispatches, and the benchmark_runs row itself.
    Used when the user explicitly clears a live session so it doesn't
    silently reappear (e.g. in the Timeline runs list) after a page refresh."""
    conn = get_conn()
    conn.execute("DELETE FROM incidents WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM resources WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM agent_messages WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM conflicts WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM dispatches WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM benchmark_runs WHERE run_id=?", (run_id,))
    conn.commit()
    conn.close()


def compare_runs(multi_run_id, single_run_id):
    multi = get_benchmark_run(multi_run_id)
    single = get_benchmark_run(single_run_id)
    if not multi or not single:
        return None

    def pct_diff(a, b):
        if b in (None, 0):
            return None
        return round(((a - b) / b) * 100, 1)

    multi_q = multi.get("quality_score") or 0
    single_q = single.get("quality_score") or 0

    return {
        "multi_agent": multi,
        "single_agent": single,
        "improvements": {
            "quality_score_diff": round(multi_q - single_q, 1),
            "quality_score_pct": pct_diff(multi_q, single_q),
            "priority_violations_avoided": (single.get("priority_violations") or 0) - (multi.get("priority_violations") or 0),
            "double_assignments_avoided": (single.get("double_assignments") or 0) - (multi.get("double_assignments") or 0),
            "escalated_incidents_diff": (single.get("escalated_incidents") or 0) - (multi.get("escalated_incidents") or 0),
            "response_time_diff_seconds": round(
                (multi.get("avg_response_time_seconds") or 0) - (single.get("avg_response_time_seconds") or 0), 1
            ),
        }
    }
