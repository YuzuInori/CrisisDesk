# CrisisDesk

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

**Qwen Cloud Hackathon — Track 3: Agent Society**

Multi-agent AI dispatch simulation (Triage → Allocator ↔ Auditor → Liaison) benchmarked
against a single-agent baseline, built on the Qwen API.

## Proof of Alibaba Cloud Deployment

**1. Code file using a Qwen Cloud base URL** — [`backend/orchestrator/orchestrator.py`](backend/orchestrator/orchestrator.py)
configures the `AsyncOpenAI` client against Alibaba Cloud's Dashscope service:

```python
QWEN_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
```

This matches the required base URL for the OpenAI-compatible plan. Every Triage,
Allocator, Auditor, Liaison, and single-agent-baseline call in this project routes
through that endpoint.

**2. Screenshot of running resources on Alibaba Cloud Workbench:**

![Alibaba Cloud ECS Workbench](frontend/alibaba-cloud-screenshot.png)

**3. Live deployment:** the backend runs on an Alibaba Cloud ECS instance —
see [Deployment](#3-deploy-to-alibaba-cloud-ecs) below for the exact setup.

## Project layout

```
main.py                    # entrypoint
requirements.txt
.env.example
crisisdesk.service         # systemd unit template — see Deployment
nginx-crisisdesk.conf      # nginx reverse proxy template — see Deployment
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
  logo1.png                  # logo — served at /static/logo1.png
docs/
  alibaba-cloud-screenshot.png   # proof-of-deployment screenshot (see above)
data/                       # SQLite database lives here at runtime (gitignored)
```

## 1. Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and paste your real QWEN_API_KEY (and optionally the per-role
# keys / QWEN_MAX_CONCURRENCY — see the comments in .env.example)

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
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env       # paste your real QWEN_API_KEY
```

### 3.3 Run it as a systemd service (so it survives reboots/crashes)

This repo includes `crisisdesk.service` at the root — copy it to
`/etc/systemd/system/`, double-check the `WorkingDirectory` and `ExecStart`
paths match where you cloned the repo, then:

```bash
sudo cp crisisdesk.service /etc/systemd/system/crisisdesk.service
sudo systemctl daemon-reload
sudo systemctl enable crisisdesk
sudo systemctl start crisisdesk
sudo systemctl status crisisdesk   # should show "active (running)"
```

Logs: `sudo journalctl -u crisisdesk -f`

### 3.4 Put nginx in front of it (recommended)

Running uvicorn directly on port 8000 works, but a reverse proxy lets you use
port 80/443 and avoids exposing the raw app port. This repo includes
`nginx-crisisdesk.conf` at the root — copy it in, edit `server_name` to your
domain or ECS public IP, then:

```bash
sudo cp nginx-crisisdesk.conf /etc/nginx/sites-available/crisisdesk
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
cd <your-repo>
git pull
.venv/bin/pip install -r requirements.txt   # only if deps changed
sudo systemctl restart crisisdesk
```

## Submission checklist

- [x] Public, open-source code repo (MIT `LICENSE`, visible in the About sidebar)
- [x] Code file with the Qwen Cloud base URL clearly visible (`backend/orchestrator/orchestrator.py`)
- [ ] Screenshot showing proof of deployment on Alibaba Cloud (`docs/alibaba-cloud-screenshot.png`)
- [ ] 3-minute demo video of the real running app (not a mockup)
- [x] Track identified: Track 3 — Agent Society

## Notes

- The SQLite database lives at `data/crisisdesk.db` — back it up before big
  changes if you care about run history; it's gitignored so it won't be
  overwritten by a `git pull`.
- `DEV_MODE=1` in `.env` enables uvicorn's autoreload for local development —
  leave it unset in production.
