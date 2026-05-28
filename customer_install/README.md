# Orby Customer Install

This is what gets installed on each business's box. Lean, ~10MB, four modules, three-tier LLM failover.

## Directory layout

```
/opt/orbi/                       (this directory once installed)
├── orbi.py                      ← main Flask service
├── llm_client.py                ← three-tier failover
├── prompts.py                   ← system prompts (public + owner)
├── auth.py                      ← owner login + session cookies
├── watchdog.py                  ← copied from /home/frank/orbi_web/watchdog/
├── config.json                  ← per-install config (start from config.json.template)
├── requirements.txt
│
├── modules/
│   ├── __init__.py
│   ├── business_info.py
│   ├── memory.py
│   ├── notes.py
│   └── messages.py
│
├── data/                        ← customer's data (NEVER LEAVES THIS BOX)
│   ├── business_info.json
│   ├── memory.json
│   ├── notes.json
│   ├── messages.json
│   ├── .session_secret          ← auto-generated, 0600 perms
│   └── ...
│
├── static/                      ← public chat shell
│   ├── chat.html
│   ├── chat.css
│   └── chat.js
│
├── owner_dashboard/             ← copied from /home/frank/orbi_web/owner_dashboard/
│   ├── login.html
│   ├── dashboard.html
│   ├── dashboard.css
│   └── dashboard.js
│
├── pwa/                         ← copied from /home/frank/orbi_web/pwa/
│   ├── manifest.json
│   ├── service-worker.js
│   ├── install-prompt.js
│   ├── offline.html
│   └── icons/
│
├── snapshots/                   ← watchdog backups, rotated daily
├── llm_local/                   ← Llama-3.2-3B GGUF + binary for offline tier
└── tunnel/                      ← cloudflared config
```

## Install on a fresh customer box

```bash
# 1. System prep (Ubuntu 24.04 LTS)
sudo apt update
sudo apt install -y python3 python3-pip python3-venv tar

# 2. Create user + directory
sudo useradd -r -s /usr/sbin/nologin -d /opt/orbi orbi 2>/dev/null || true
sudo mkdir -p /opt/orbi
sudo chown -R orbi:orbi /opt/orbi

# 3. Copy the install
sudo -u orbi cp -r /path/to/orbi_web/customer_install/* /opt/orbi/
sudo -u orbi cp -r /path/to/orbi_web/pwa /opt/orbi/
sudo -u orbi cp -r /path/to/orbi_web/owner_dashboard /opt/orbi/
sudo -u orbi cp    /path/to/orbi_web/watchdog/watchdog.py /opt/orbi/

# 4. Install Python deps
sudo pip3 install -r /opt/orbi/requirements.txt

# 5. Create config.json from template
sudo -u orbi cp /opt/orbi/config.json.template /opt/orbi/config.json
sudo nano /opt/orbi/config.json     # fill in real values

# 6. Initialize data dir
sudo -u orbi cp /opt/orbi/data/business_info.json.template /opt/orbi/data/business_info.json
sudo -u orbi cp /opt/orbi/data/memory.json.template /opt/orbi/data/memory.json
sudo -u orbi cp /opt/orbi/data/notes.json.template /opt/orbi/data/notes.json
sudo -u orbi cp /opt/orbi/data/messages.json.template /opt/orbi/data/messages.json

# 7. Set owner password (interactive)
sudo -u orbi python3 -c "
import json, getpass, sys
sys.path.insert(0, '/opt/orbi')
import auth
cfg = json.load(open('/opt/orbi/config.json'))
email = input('Owner email: ').strip()
pw = getpass.getpass('Owner password: ')
cfg['owner']['email'] = email
cfg['owner']['_password_hash'] = auth.hash_password(pw)
json.dump(cfg, open('/opt/orbi/config.json','w'), indent=2)
print('Owner credentials saved.')
"

# 8. Install systemd units (from /home/frank/orbi_web/watchdog/)
sudo cp /home/frank/orbi_web/watchdog/orbi.service /etc/systemd/system/
sudo cp /home/frank/orbi_web/watchdog/orbi-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orbi.service orbi-watchdog.service

# 9. Verify
systemctl status orbi orbi-watchdog
curl http://127.0.0.1:5050/health
```

## What runs after install

- `orbi.service` — the Flask service (port 5050)
- `orbi-watchdog.service` — health monitor + auto-restart + snapshot rollback
- (later) `cloudflared.service` — exposes the box to the internet through their tunnel URL
- (later) Local llama-server on port 11435 for the offline fallback tier

## Health endpoint

```bash
curl http://127.0.0.1:5050/health
# → {"status":"ok","version":"0.1.0","uptime":1234,"billing":true,"business_name":"Joe's Pizza"}
```

## Testing the LLM tiers

```bash
# Tier 1 (brain) — should answer when brain is reachable
curl -X POST http://127.0.0.1:5050/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What time do you open tomorrow?"}'

# Tier 2 (HF) — block brain by adding a bad hostname to /etc/hosts
echo "127.0.0.1 orbi-brain.frank.com" | sudo tee -a /etc/hosts
# ...test, then remove the line

# Tier 3 (local) — disable HF and block brain to force local
```
