# Orbi Watchdog

Self-healing supervisor for the customer-side Orbi install.

## What it does

1. Pings `http://127.0.0.1:5050/health` every 30 seconds.
2. After 3 consecutive failures, restarts the Orbi service via systemctl.
3. After 3 failed restarts, rolls back to the most recent snapshot.
4. Continues trying older snapshots if the newest one doesn't restore cleanly.
5. Daily snapshot at 03:00 local time, keeps the last 7.
6. Pre-update snapshot whenever `/opt/orbi/UPDATING.lock` appears.
7. Notifies the owner (via Orbi's push endpoint) on rollback or unrecoverable failure.

## Install

```bash
# 1. Copy files into place
sudo cp watchdog.py /opt/orbi/watchdog.py
sudo cp orbi.service /etc/systemd/system/orbi.service
sudo cp orbi-watchdog.service /etc/systemd/system/orbi-watchdog.service
sudo chmod 644 /etc/systemd/system/orbi*.service
sudo chmod 755 /opt/orbi/watchdog.py

# 2. Create the orbi user if it doesn't exist
sudo useradd -r -s /usr/sbin/nologin -d /opt/orbi orbi 2>/dev/null || true
sudo chown -R orbi:orbi /opt/orbi

# 3. Enable + start both services
sudo systemctl daemon-reload
sudo systemctl enable --now orbi.service
sudo systemctl enable --now orbi-watchdog.service

# 4. Verify
systemctl status orbi orbi-watchdog
journalctl -u orbi-watchdog -f
```

## Environment overrides

The watchdog reads its config from environment variables (override in the service file or `/etc/default/orbi-watchdog`):

| Variable | Default | What it does |
|----------|---------|--------------|
| `ORBI_DIR` | `/opt/orbi` | Install root |
| `ORBI_HEALTH_URL` | `http://127.0.0.1:5050/health` | Endpoint to poll |
| `ORBI_SERVICE` | `orbi` | systemd unit to restart |
| `ORBI_CHECK_INTERVAL` | `30` | Seconds between checks |
| `ORBI_HEALTH_TIMEOUT` | `10` | Health-check timeout |
| `ORBI_FAIL_THRESHOLD` | `3` | Failures before action |
| `ORBI_MAX_RESTARTS` | `3` | Restarts before rollback |
| `ORBI_RESTART_BACKOFF` | `30` | Seconds between restart attempts |
| `ORBI_SNAPSHOT_RETAIN` | `7` | Days of snapshots to keep |
| `ORBI_SNAPSHOT_HOUR` | `3` | Hour (0-23) for daily snapshot |

## Manual snapshot operations

Take a snapshot right now (e.g. before a manual change):

```bash
touch /opt/orbi/UPDATING.lock
# Wait ~30s for the watchdog to notice
rm /opt/orbi/UPDATING.lock
```

List snapshots:

```bash
ls -lh /opt/orbi/snapshots/
```

Manual restore (if needed):

```bash
sudo systemctl stop orbi
sudo -u orbi tar -xzf /opt/orbi/snapshots/orbi-daily-YYYYMMDD-HHMMSS.tar.gz -C /opt/orbi/
sudo systemctl start orbi
```

## Logs

```bash
journalctl -u orbi-watchdog -f          # live tail
tail -f /opt/orbi/watchdog.log          # local log file
```
