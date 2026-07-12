# CrisisDesk

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

**Qwen Cloud Hackathon — Track 3: Agent Society**

Multi-agent AI dispatch simulation (Triage → Allocator ↔ Auditor → Liaison) benchmarked
against a single-agent baseline, built on the Qwen API.

## Proof of Alibaba Cloud usage

- **Backend runtime**: deployed on Alibaba Cloud ECS — see [Deployment](#3-deploy-to-alibaba-cloud-ecs) below.
- **API usage in code**: [`backend/orchestrator/orchestrator.py`](backend/orchestrator/orchestrator.py)
  calls the Qwen model through Alibaba Cloud's Dashscope service
  (`QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"`),
  via the `AsyncOpenAI` client configured near the top of that file.

## Project layout

```
main.py                    # entrypoint
backend/
  api/routes.py            # FastAPI routes + websocket
  agents/agents.py         # prompt builders + response parsers for each agent role
  orchestrator/
    orchestrator.py        # the actual multi-agent / single-agent pipelines
    benchmark.py            # benchmark scenario runner + quality scoring
  simulation/world.py       # incident types, rulebook, map/scenario generation
  db/database.py            # SQLite persistence
frontend/
  index.html                 # the whole dashboard (map, timeline, benchmark, live session)
  logo1.png                  # your logo — served at /static/logo1.png
data/                       # SQLite database lives here at runtime (gitignored)
```

## 1. Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp _env.example .env
# edit .env and paste your real QWEN_API_KEY (and optionally the per-role
# keys / QWEN_MAX_CONCURRENCY — see the comments in _env.example)

python main.py
```

Visit `http://localhost:8000`.

## 2. Push to GitHub

```bash
git init
git add .
git commit -m "CrisisDesk — multi-agent dispatch simulation"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

`.gitignore` already excludes `.env` and the SQLite database — double-check
`git status` before your first commit that no real API key is staged.

## 3. Deploy to Alibaba Cloud (ECS)

These steps assume a plain Ubuntu ECS instance you can SSH into.

### 3.1 Server prerequisites

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git nginx
```

### 3.2 Get the code onto the server

```bash
cd /opt
sudo git clone https://github.com/<your-username>/<your-repo>.git crisisdesk
cd crisisdesk
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp _env.example .env
sudo nano .env       # paste your real QWEN_API_KEY
```

### 3.3 Run it as a systemd service (so it survives reboots/crashes)

Copy `deploy/crisisdesk.service` (included in this repo) to
`/etc/systemd/system/crisisdesk.service`, double-check the `WorkingDirectory`
and `ExecStart` paths match where you cloned the repo, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable crisisdesk
sudo systemctl start crisisdesk
sudo systemctl status crisisdesk   # should show "active (running)"
```

Logs: `sudo journalctl -u crisisdesk -f`

### 3.4 Put nginx in front of it (recommended)

Running uvicorn directly on port 8000 works, but a reverse proxy lets you use
port 80/443, add TLS, and avoids exposing the raw app port. Copy
`deploy/nginx-crisisdesk.conf` to `/etc/nginx/sites-available/crisisdesk`,
edit `server_name` to your domain or the ECS public IP, then:

```bash
sudo ln -s /etc/nginx/sites-available/crisisdesk /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 3.5 Open the firewall

In the Alibaba Cloud console, add a Security Group inbound rule allowing TCP
80 (and 443 if you set up TLS) from `0.0.0.0/0`. If you're skipping nginx and
hitting uvicorn directly, open 8000 instead.

### 3.6 Updating after a new push

```bash
cd /opt/crisisdesk
sudo git pull
sudo .venv/bin/pip install -r requirements.txt   # only if deps changed
sudo systemctl restart crisisdesk
```

## Notes

- The SQLite database lives at `data/crisisdesk.db` — back it up before big
  changes if you care about run history; it's gitignored so it won't be
  overwritten by a `git pull`.
- `DEV_MODE=1` in `.env` enables uvicorn's autoreload for local development —
  leave it unset in production.
