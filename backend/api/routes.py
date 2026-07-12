"""
CrisisDesk FastAPI Routes
"""
import asyncio
import random
import uuid
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from backend.db.database import (
    init_db, get_incidents_by_run, get_resources_by_run,
    get_messages_by_run, get_conflicts_by_run, get_dispatches_by_run,
    get_benchmark_run, get_all_benchmark_runs, compare_runs,
    insert_incident, insert_resource, get_available_resources, create_benchmark_run,
    delete_run,
)
from backend.orchestrator.benchmark import run_benchmark_scenario, run_full_comparison
from backend.orchestrator.orchestrator import process_incident_batch_multi_agent
from backend.simulation.world import generate_incident, INCIDENT_TYPES, resource_need_label

app = FastAPI(title="CrisisDesk", version="2.0.0")
init_db()


# ─── WebSocket Manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


@app.websocket("/ws/live")
async def live_feed(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        manager.disconnect(websocket)


async def ws_emit(event: dict):
    await manager.broadcast(event)


# ─── Live Session ──────────────────────────────────────────────────────────────

class LiveSession:
    def __init__(self):
        self.run_id: Optional[str] = None
        self.resource_counts = {"ambulance": 2, "fire_truck": 1, "rescue_boat": 1}
        self.declared_count = 0
        self.active_tasks: set = set()

    def ensure_run(self):
        if not self.run_id:
            self.run_id = f"live-{uuid.uuid4().hex[:8]}"
            create_benchmark_run(self.run_id, "multi_agent", "live_session")
            self._seed_resources()
        return self.run_id

    def _seed_resources(self):
        import random
        for rtype, n in self.resource_counts.items():
            for i in range(1, n + 1):
                insert_resource(
                    f"{self.run_id}-{rtype.upper()}-{i}", self.run_id, rtype,
                    round(random.uniform(0, 100), 1), round(random.uniform(0, 100), 1)
                )

    def stop(self):
        n = len(self.active_tasks)
        for t in list(self.active_tasks):
            t.cancel()
        self.active_tasks.clear()
        return n

    def reset(self):
        n = self.stop()
        self.run_id = None
        self.declared_count = 0
        return n


live_session = LiveSession()


# ─── Pydantic Models ────────────────────────────────────────────────────────────

class RunScenarioRequest(BaseModel):
    scenario_name: str = "random"
    mode: str = "multi_agent"
    resource_counts: Optional[dict] = None
    seed: Optional[int] = None


class CompareRequest(BaseModel):
    scenario_name: str = "random"
    resource_counts: Optional[dict] = None
    seed: Optional[int] = None


class ResourceSettingsRequest(BaseModel):
    ambulance: int = 2
    fire_truck: int = 1
    rescue_boat: int = 1


class DeclareIncidentsRequest(BaseModel):
    count: int
    incident_type: Optional[str] = None
    incident_types: Optional[List[Optional[str]]] = None


# ─── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "CrisisDesk", "version": "2.0.0"}


# ─── Benchmark Endpoints ───────────────────────────────────────────────────────

@app.post("/api/run")
async def run_scenario(req: RunScenarioRequest):
    if req.mode not in ("multi_agent", "single_agent"):
        raise HTTPException(400, "mode must be 'multi_agent' or 'single_agent'")
    resource_counts = req.resource_counts or live_session.resource_counts
    return await run_benchmark_scenario(
        req.mode, req.scenario_name, ws_callback=ws_emit,
        resource_counts=resource_counts, seed=req.seed,
    )


@app.post("/api/compare")
async def compare_scenario(req: CompareRequest):
    resource_counts = req.resource_counts or live_session.resource_counts
    return await run_full_comparison(
        req.scenario_name, ws_callback=ws_emit,
        resource_counts=resource_counts, seed=req.seed,
    )


# ─── Live Session ──────────────────────────────────────────────────────────────

@app.get("/api/live/settings")
def get_live_settings():
    open_incidents = 0
    if live_session.run_id:
        all_incidents = get_incidents_by_run(live_session.run_id)
        open_incidents = sum(
            1 for i in all_incidents if i["status"] not in ("resolved", "escalated_no_resource")
        )
    return {
        "resource_counts": live_session.resource_counts,
        "run_id": live_session.run_id,
        "declared_count": live_session.declared_count,
        "active": live_session.run_id is not None,
        "processing": len(live_session.active_tasks) > 0,
        "open_incidents": open_incidents,
    }


@app.post("/api/live/settings")
def set_live_settings(req: ResourceSettingsRequest):
    new_counts = {
        "ambulance": max(0, min(9, req.ambulance)),
        "fire_truck": max(0, min(9, req.fire_truck)),
        "rescue_boat": max(0, min(9, req.rescue_boat)),
    }

    if live_session.run_id is None:
        # No active session yet — just store the target counts, seeded on first declare.
        live_session.resource_counts = new_counts
        return {"status": "ok", "resource_counts": live_session.resource_counts}

    # A session is already active: allow adding MORE resources on the fly by
    # seeding the extra units directly into the current run — no need to
    # Reset just to bump a count. Decreasing is still blocked: removing
    # resources that might already be dispatched would corrupt live state.
    added = {}
    for rtype, new_n in new_counts.items():
        old_n = live_session.resource_counts.get(rtype, 0)
        if new_n < old_n:
            raise HTTPException(
                400,
                f"Can't reduce {rtype.replace('_', ' ')} count while a session is active "
                f"(resources may already be in use). You can still increase counts, "
                f"or Reset to start over."
            )
        if new_n > old_n:
            for i in range(old_n + 1, new_n + 1):
                rid = f"{live_session.run_id}-{rtype.upper()}-{i}"
                insert_resource(rid, live_session.run_id, rtype,
                                round(random.uniform(0, 100), 1), round(random.uniform(0, 100), 1))
            added[rtype] = new_n - old_n

    live_session.resource_counts = new_counts
    return {"status": "ok", "resource_counts": live_session.resource_counts, "added": added}


@app.post("/api/live/declare")
async def declare_incidents(req: DeclareIncidentsRequest):
    count = max(0, min(9, req.count))
    if count == 0:
        raise HTTPException(400, "count must be 1-9")

    run_id = live_session.ensure_run()
    batch = []
    preview = []

    default_type = req.incident_type if req.incident_type in INCIDENT_TYPES else None
    # incident_types[i] lets each slot in the batch get its own explicit type
    # (e.g. from the "pick a type per incident" declare modal); falls back to
    # the single incident_type / random for any slot left unspecified.
    per_slot_types = req.incident_types or []

    for i in range(count):
        slot_type = per_slot_types[i] if i < len(per_slot_types) else None
        chosen_type = slot_type if slot_type in INCIDENT_TYPES else default_type
        inc = generate_incident(chosen_type)
        idx = live_session.declared_count + 1
        incident_id = f"{run_id}-INC-{idx:03d}"
        insert_incident(incident_id, run_id, inc["incident_type"],
                        inc["incident_type"].replace("_", " ").title(), inc["x"], inc["y"])
        live_session.declared_count += 1

        incident_payload = {
            "incident_id": incident_id,
            "incident_type": inc["incident_type"],
            "description": inc["incident_type"].replace("_", " ").title(),
            "x": inc["x"], "y": inc["y"],
        }
        batch.append(incident_payload)
        preview.append({
            "incident_id": incident_id,
            "incident_type": inc["incident_type"],
            "needs": resource_need_label(inc["incident_type"]),
            "x": inc["x"], "y": inc["y"],
        })

    # Tell the UI immediately what each incident needs, before the agents even
    # start reasoning — this is what lets you watch resource contention play out.
    await ws_emit({"type": "incidents_declared", "run_id": run_id, "incidents": preview})

    # Process this batch CONCURRENTLY in the background. The HTTP call returns
    # right away — it does NOT wait for the agents to finish — so you can
    # declare more incidents (or Stop) while this batch is still being decided.
    task = asyncio.create_task(_run_live_batch(run_id, batch))
    live_session.active_tasks.add(task)
    task.add_done_callback(lambda t: live_session.active_tasks.discard(t))

    return {
        "run_id": run_id,
        "declared_this_batch": count,
        "total_declared": live_session.declared_count,
        "incidents": preview,
    }


async def _run_live_batch(run_id: str, batch: list):
    try:
        await process_incident_batch_multi_agent(batch, run_id, ws_emit)
        await ws_emit({"type": "batch_complete", "run_id": run_id,
                       "message": f"✅ Batch of {len(batch)} incident(s) fully processed."})
    except asyncio.CancelledError:
        await ws_emit({"type": "batch_stopped", "run_id": run_id,
                       "message": "⏹ Stopped — this batch was cancelled mid-decision."})
        raise
    except Exception as e:
        await ws_emit({"type": "batch_error", "run_id": run_id, "message": f"⚠️ Error: {e}"})


@app.post("/api/live/stop")
async def stop_live_session():
    if not live_session.run_id:
        return {"status": "ok", "stopped": 0}
    n = live_session.stop()
    await ws_emit({"type": "session_stopped", "run_id": live_session.run_id,
                   "message": f"⏹ Stopped — {n} in-flight batch(es) cancelled."})
    return {"status": "ok", "stopped": n}


@app.post("/api/live/reset")
async def reset_live_session():
    old_run_id = live_session.run_id
    n = live_session.reset()
    if old_run_id:
        delete_run(old_run_id)
    if n:
        await ws_emit({"type": "session_stopped", "message": f"⏹ Reset — {n} in-flight batch(es) cancelled."})
    await ws_emit({"type": "session_reset", "run_id": old_run_id, "message": "🗑 Live session cleared."})
    return {"status": "ok"}


# ─── Run Inspection ───────────────────────────────────────────────────────────

@app.get("/api/runs")
def list_runs():
    return get_all_benchmark_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = get_benchmark_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        "run": run,
        "incidents": get_incidents_by_run(run_id),
        "conflicts": get_conflicts_by_run(run_id),
        "dispatches": get_dispatches_by_run(run_id),
        "resources": get_resources_by_run(run_id),
    }


@app.get("/api/runs/{run_id}/transcript")
def get_transcript(run_id: str, incident_id: Optional[str] = None):
    return get_messages_by_run(run_id, incident_id)


@app.get("/api/incident-types")
def list_incident_types():
    return INCIDENT_TYPES


# ─── Frontend ─────────────────────────────────────────────────────────────────

frontend_path = Path(__file__).parent.parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_path)), name="static")


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/dashboard/{path:path}", response_class=HTMLResponse)
async def serve_dashboard(path: str = ""):
    index = frontend_path / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse("<h1>CrisisDesk — Frontend not built yet</h1>")
