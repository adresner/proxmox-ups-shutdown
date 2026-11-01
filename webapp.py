#!/usr/bin/env python3
"""Flask web UI for the UPS shutdown orchestrator.

Read-only-by-default operations: view UPS, view config, simulate, test SSH.
Mutating operations (add/remove/enable/disable host, set threshold, reset state)
require HTTP Basic Auth using web.username / web.password from config.json.

No real shutdowns are exposed in the UI on purpose. The cron worker does that
when the UPS itself reports a real outage.
"""
from __future__ import annotations

import functools
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from flask import (
    Flask, jsonify, request, render_template_string, Response,
    session, redirect, url_for,
)

import pmlib

app = Flask(__name__)

# Sign session cookies with a key kept in a 0600 file next to webapp.py.
# Auto-generated on first run; survives restarts so users stay logged in.
_KEY_PATH = Path(__file__).parent / ".session_secret"
if not _KEY_PATH.exists():
    _KEY_PATH.write_bytes(os.urandom(32))
    os.chmod(_KEY_PATH, 0o600)
app.secret_key = _KEY_PATH.read_bytes()
app.permanent_session_lifetime = timedelta(days=30)


# --- Auth ------------------------------------------------------------------

def _check_auth(username, password) -> bool:
    cfg = pmlib.load_config()
    web = cfg.get("web", {})
    return username == web.get("username") and password == web.get("password")


def _is_logged_in() -> bool:
    """True if the request carries a valid session cookie or Basic Auth.

    Sessions are the browser path. Basic Auth is preserved as a fallback so
    curl/scripts still work without going through the login form.
    """
    if session.get("user"):
        return True
    auth = request.authorization
    if auth and _check_auth(auth.username, auth.password):
        return True
    return False


def requires_auth(view):
    """Require auth. API routes return JSON 401; pages redirect to /login."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if _is_logged_in():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "not authenticated"}), 401
        return redirect(url_for("login", next=request.path))
    return wrapper


# --- Login / logout --------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if _check_auth(username, password):
            session.permanent = True
            session["user"] = username
            dest = request.args.get("next") or "/"
            if not dest.startswith("/") or dest.startswith("//"):
                dest = "/"
            return redirect(dest)
        error = "Invalid username or password."
    if _is_logged_in() and request.method == "GET":
        return redirect("/")
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Read endpoints --------------------------------------------------------

@app.route("/")
@requires_auth
def index():
    return render_template_string(PAGE)


@app.route("/api/status")
@requires_auth
def api_status():
    cfg = pmlib.load_config()
    try:
        ups = pmlib.read_ups(cfg["ups_name"])
        ups_ok = True
        ups_err = None
    except Exception as e:
        ups = {}
        ups_ok = False
        ups_err = str(e)
    return jsonify({
        "ups_ok": ups_ok,
        "ups_err": ups_err,
        "ups_status": ups.get("ups.status", "?"),
        "charge": ups.get("_charge", 0),
        "runtime_s": ups.get("_runtime_s", 0),
        "load_pct": ups.get("_load_pct"),
        "realpower_w": ups.get("_realpower_w"),
        "model": ups.get("device.model", "?"),
        "state": pmlib.read_state(cfg),
        "thresholds": pmlib.thresholds(cfg),
        "effective_mode": pmlib.effective_mode(cfg, ups.get("_runtime_s", 0)),
        "ups_name": cfg["ups_name"],
        "hosts": [h for h in cfg["hosts"]],
        "log_tail": pmlib.tail_log(cfg, lines=30),
        "worker_age_s": pmlib.heartbeat_age(cfg),
        "worker_stale_s": pmlib.WORKER_STALE_S,
        "selftest_ok": (pmlib.load_selftest(cfg) or {}).get("ok"),
    })


# --- Self-test -------------------------------------------------------------

@app.route("/api/selftest", methods=["GET"])
@requires_auth
def api_selftest_get():
    cfg = pmlib.load_config()
    return jsonify({
        "config": pmlib.selftest_cfg(cfg),
        "last": pmlib.load_selftest(cfg),
        "server_time": time.strftime("%H:%M"),
        "server_tz": time.strftime("%Z"),
        "worker_age_s": pmlib.heartbeat_age(cfg),
    })


@app.route("/api/selftest/config", methods=["POST"])
@requires_auth
def api_selftest_config():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    try:
        hour = int(data.get("hour", 7))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "hour must be an integer"}), 400
    if not 0 <= hour <= 23:
        return jsonify({"ok": False, "msg": "hour must be 0-23"}), 400
    cfg["selftest"] = {
        "enabled": bool(data.get("enabled", True)),
        "hour": hour,
        "email_on_success": bool(data.get("email_on_success", False)),
    }
    pmlib.save_config(cfg)
    return jsonify({"ok": True, "msg": "saved"})


@app.route("/api/selftest/run", methods=["POST"])
@requires_auth
def api_selftest_run():
    """Run the self-test now. Read-only checks; never emails (you're watching)."""
    cfg = pmlib.load_config()
    result = pmlib.run_selftest(cfg, trigger="manual")
    pmlib.save_selftest(cfg, result)
    return jsonify(result)


def _worker_watchdog() -> None:
    """Email if the cron worker stops running. Runs in the web process because
    a dead worker can't report on itself. One alert per episode, plus a
    recovery email."""
    started = time.time()
    alerted = False
    while True:
        time.sleep(60)
        try:
            cfg = pmlib.load_config()
            if not pmlib.selftest_cfg(cfg)["enabled"]:
                continue
            age = pmlib.heartbeat_age(cfg)
            # After a reboot /var/run is empty; give the worker time to tick.
            stale = (age is None and time.time() - started > pmlib.WORKER_STALE_S) \
                or (age is not None and age > pmlib.WORKER_STALE_S)
            log = pmlib.get_logger(cfg)
            if stale and not alerted:
                what = "has never run since boot" if age is None else f"last ran {age // 60} min ago"
                ok, msg = pmlib.send_email(
                    cfg, "[nutshutdown] WORKER NOT RUNNING: no UPS monitoring",
                    f"The power_monitor.py cron worker {what}.\n\nWhile it is "
                    f"stopped, a power outage will NOT trigger any shutdowns.\n"
                    f"Check `crontab -l` and run `/usr/bin/python3 "
                    f"/root/power_monitor.py` by hand to see the error.\n")
                log.error(f"WATCHDOG: worker {what}; alert email "
                          f"{'sent' if ok else 'FAILED: ' + msg}")
                alerted = ok
            elif not stale and alerted:
                pmlib.send_email(cfg, "[nutshutdown] Worker running again",
                                 f"The cron worker is running again (last ran {age}s ago).\n")
                log.info("WATCHDOG: worker running again")
                alerted = False
        except Exception:
            pass  # never let the watchdog take the web UI down


@app.route("/api/simulate", methods=["POST"])
@requires_auth
def api_simulate():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    charge = int(data.get("charge", 100))
    runtime_s = None
    if data.get("runtime_min") not in (None, ""):
        runtime_s = int(float(data["runtime_min"]) * 60)
    force_ob = bool(data.get("on_battery", True))
    status = "OB" if force_ob else "OL"
    state = pmlib.read_state(cfg)
    action = pmlib.decide_action(cfg, status, charge, state, runtime_s=runtime_s)
    detail = []
    if action == "shutdown_guests":
        detail = pmlib.shutdown_guests(cfg, dry_run=True)
    elif action == "shutdown_hosts":
        detail = pmlib.shutdown_hosts(cfg, dry_run=True)
    return jsonify({"status": status, "charge": charge, "runtime_s": runtime_s,
                    "mode_used": pmlib.effective_mode(cfg, runtime_s),
                    "state": state, "action": action, "detail": detail})


@app.route("/api/stats")
@requires_auth
def api_stats():
    """Outage history parsed from the worker log (drain rate, durations)."""
    return jsonify(pmlib.outage_history(pmlib.load_config()))


@app.route("/api/ups/config", methods=["GET"])
@requires_auth
def api_ups_config_get():
    cfg = pmlib.load_config()
    return jsonify(pmlib.get_ups_config(cfg))


@app.route("/api/notifications/config", methods=["GET"])
@requires_auth
def api_notifications_config_get():
    cfg = pmlib.load_config()
    n = cfg.get("notifications", {}) or {}
    return jsonify({
        "smtp_host": n.get("smtp_host", ""),
        "smtp_port": n.get("smtp_port", 25),
        "smtp_user": n.get("smtp_user", ""),
        "smtp_password": n.get("smtp_password", ""),
        "smtp_use_tls": bool(n.get("smtp_use_tls", False)),
        "from_address": n.get("from_address", ""),
        "to_addresses": n.get("to_addresses", []),
        "warning_pct": n.get("warning_pct", 80),
        "events": {
            "warning": bool((n.get("events") or {}).get("warning", True)),
            "guests_shutdown": bool((n.get("events") or {}).get("guests_shutdown", True)),
            "hosts_shutdown": bool((n.get("events") or {}).get("hosts_shutdown", True)),
            "power_restored": bool((n.get("events") or {}).get("power_restored", True)),
        },
    })


@app.route("/api/notifications/config", methods=["POST"])
@requires_auth
def api_notifications_config_post():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    err = pmlib.validate_notifications_input(data)
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    events_in = data.get("events") or {}
    cfg.setdefault("notifications", {})
    cfg["notifications"].update({
        "smtp_host": (data.get("smtp_host") or "").strip(),
        "smtp_port": int(data.get("smtp_port") or 25),
        "smtp_user": (data.get("smtp_user") or "").strip(),
        "smtp_password": data.get("smtp_password") or "",
        "smtp_use_tls": bool(data.get("smtp_use_tls", False)),
        "from_address": (data.get("from_address") or "").strip(),
        "to_addresses": pmlib._parse_recipients(data.get("to_addresses")),
        "warning_pct": int(data.get("warning_pct") or 80),
        "events": {k: bool(events_in.get(k, True)) for k in pmlib.EVENT_NAMES},
    })
    pmlib.save_config(cfg)
    return jsonify({"ok": True, "msg": "saved"})


@app.route("/api/notifications/test", methods=["POST"])
@requires_auth
def api_notifications_test():
    cfg = pmlib.load_config()
    subject = "[nutshutdown] test email"
    body = ("This is a test email from the nutshutdown web UI.\n\n"
            "If you got this, your SMTP settings work and alerts will fire on a "
            "real outage.\n")
    ok, msg = pmlib.send_email(cfg, subject, body)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/ups/config", methods=["POST"])
@requires_auth
def api_ups_config_post():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    ok, msg = pmlib.update_ups_config(cfg, {
        "name": (data.get("name") or "").strip(),
        "host": (data.get("host") or "").strip(),
        "community": (data.get("community") or "").strip(),
        "pollfreq": data.get("pollfreq"),
    })
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.route("/api/host/<ip>/push-key", methods=["POST"])
@requires_auth
def api_push_key(ip):
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    password = data.get("password") or ""
    if not password:
        return jsonify({"ok": False, "msg": "password required"}), 400
    # Allow ad-hoc IPs not yet in config (so "add + push" works in one flow).
    match = next((pmlib.Host.from_dict(h) for h in cfg["hosts"] if h["ip"] == ip),
                 None)
    host = match or pmlib.Host(ip=ip, label=ip, enabled=False)
    ok, msg = pmlib.push_ssh_key(host, cfg, password)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/test-ssh", methods=["POST"])
@requires_auth
def api_test_ssh():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    ip = data.get("ip")
    results = []
    targets = ([pmlib.Host.from_dict(h) for h in cfg["hosts"] if h["ip"] == ip]
               if ip else pmlib.all_hosts(cfg))
    for h in targets:
        ok, msg = pmlib.test_ssh(h, cfg)
        results.append({"ip": h.ip, "label": h.label, "ok": ok, "msg": msg})
    return jsonify({"results": results})


# --- Mutating endpoints ----------------------------------------------------

@app.route("/api/host", methods=["POST"])
@requires_auth
def api_host_add():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    ip = (data.get("ip") or "").strip()
    if not ip:
        return jsonify({"error": "ip required"}), 400
    if any(h["ip"] == ip for h in cfg["hosts"]):
        return jsonify({"error": "exists"}), 409
    cfg["hosts"].append({
        "ip": ip,
        "label": (data.get("label") or ip).strip(),
        "enabled": bool(data.get("enabled", True)),
        "note": (data.get("note") or "").strip(),
    })
    pmlib.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/host/<ip>", methods=["DELETE"])
@requires_auth
def api_host_delete(ip):
    cfg = pmlib.load_config()
    before = len(cfg["hosts"])
    cfg["hosts"] = [h for h in cfg["hosts"] if h["ip"] != ip]
    if len(cfg["hosts"]) == before:
        return jsonify({"error": "not found"}), 404
    pmlib.save_config(cfg)
    pmlib.forget_host_key(ip)
    return jsonify({"ok": True})


@app.route("/api/host/<ip>/reset-hostkey", methods=["POST"])
@requires_auth
def api_reset_hostkey(ip):
    """Forget the saved SSH host key for ip (after a reinstall)."""
    ok, msg = pmlib.forget_host_key(ip)
    fp = pmlib.scan_host_fingerprint(ip) if ok else ""
    return jsonify({"ok": ok, "msg": msg, "fingerprint": fp}), (200 if ok else 500)


@app.route("/api/host/<ip>", methods=["PATCH"])
@requires_auth
def api_host_patch(ip):
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    for h in cfg["hosts"]:
        if h["ip"] == ip:
            if "enabled" in data:
                h["enabled"] = bool(data["enabled"])
            if "label" in data:
                h["label"] = str(data["label"]).strip()
            if "note" in data:
                h["note"] = str(data["note"]).strip()
            pmlib.save_config(cfg)
            return jsonify({"ok": True, "host": h})
    return jsonify({"error": "not found"}), 404


@app.route("/api/thresholds", methods=["POST"])
@requires_auth
def api_thresholds():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    # Any key left out keeps its current value (old clients send only the %s).
    new = pmlib.thresholds(cfg)
    for k in ("mode", "vm_shutdown_pct", "host_shutdown_pct",
              "vm_shutdown_min", "host_shutdown_min"):
        if k in data:
            new[k] = data[k]
    err = pmlib.validate_thresholds(new)
    if err:
        return jsonify({"error": err}), 400
    cfg["thresholds"] = {"mode": new["mode"],
                         **{k: int(new[k]) for k in ("vm_shutdown_pct", "host_shutdown_pct",
                                                     "vm_shutdown_min", "host_shutdown_min")}}
    pmlib.save_config(cfg)
    return jsonify({"ok": True, "thresholds": cfg["thresholds"]})


@app.route("/api/state/reset", methods=["POST"])
@requires_auth
def api_state_reset():
    cfg = pmlib.load_config()
    pmlib.write_state(cfg, pmlib.NORMAL)
    return jsonify({"ok": True, "state": pmlib.NORMAL})


# --- HTML ------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>nutserver</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--ink:#e6e8ec;--muted:#8a93a6;--ok:#27c281;
      --warn:#f7b955;--bad:#ef5d5d;--accent:#6aa9ff;--line:#262a33;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,Segoe UI,Inter,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;background:#11141a}
header h1{margin:0;font-size:18px;font-weight:600}
.pill{padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600}
.pill.ok{background:#0f3a25;color:var(--ok)}
.pill.bad{background:#3a1414;color:var(--bad)}
.pill.warn{background:#3a2a0c;color:var(--warn)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px 24px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.card h2{margin:0 0 10px 0;font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.kv{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:14px}
.kv b{font-weight:500;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase}
.row-actions{display:flex;gap:6px;justify-content:flex-end}
button,input,select{font:inherit;color:inherit}
button{background:var(--accent);color:#03142e;border:0;border-radius:5px;padding:6px 10px;cursor:pointer;font-weight:600}
button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
button.danger{background:#5a1414;color:#ffd6d6}
button:disabled{opacity:.5;cursor:not-allowed}
input[type=text],input[type=number]{background:#0c0e13;border:1px solid var(--line);border-radius:5px;padding:6px 8px;color:var(--ink)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
pre.log{background:#0a0c10;border:1px solid var(--line);border-radius:6px;padding:10px;height:200px;overflow:auto;font-size:12px;color:#cdd3df;white-space:pre-wrap}
.muted{color:var(--muted)}
.bar{height:8px;background:#0a0c10;border-radius:6px;overflow:hidden;margin-top:6px}
.bar > div{height:100%;background:var(--ok)}
.bar.low > div{background:var(--warn)}
.bar.crit > div{background:var(--bad)}
.foot{padding:8px 24px;color:var(--muted);font-size:12px}
.editable{cursor:pointer;padding:2px 4px;border-radius:3px;border:1px dashed transparent;display:inline-block;min-width:80px}
.editable:hover{background:#0a0c10;border-color:var(--line)}
.placeholder{color:var(--muted);font-style:italic;font-size:12px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{background:transparent;color:var(--muted);border:0;border-radius:0;padding:5px 12px;font-weight:600}
.seg button.on{background:var(--accent);color:#03142e}
.th-pair.off{opacity:.45}
td.active,th.active{color:var(--ink);font-weight:600}
td.dim{color:var(--muted)}
</style></head><body>
<header>
  <h1>nutserver — Proxmox graceful-shutdown orchestrator</h1>
  <span id="ups-pill" class="pill">…</span>
  <span id="state-pill" class="pill warn">…</span>
  <span id="worker-pill" class="pill" title="When the every-minute cron worker last ran">…</span>
  <span id="selftest-pill" class="pill" title="Result of the last self-test">…</span>
  <span style="flex:1"></span>
  <button class="ghost" onclick="refresh()">Refresh</button>
  <form method="POST" action="/logout" style="margin:0">
    <button class="ghost" type="submit">Log out</button>
  </form>
</header>

<div class="grid">
  <section class="card">
    <h2>UPS</h2>
    <div class="kv">
      <b>Name</b><span id="ups-name">…</span>
      <b>Model</b><span id="ups-model">…</span>
      <b>Status</b><span id="ups-status">…</span>
      <b>Charge</b><span><span id="ups-charge">…</span>%<div id="ups-bar" class="bar"><div style="width:0%"></div></div></span>
      <b>Runtime</b><span id="ups-runtime">…</span>
    </div>
  </section>

  <section class="card">
    <h2>State machine</h2>
    <div class="kv">
      <b>Current</b><span id="state-text">…</span>
      <b>Guest stage</b><span id="th-vm-text">…</span>
      <b>Host stage</b><span id="th-host-text">…</span>
    </div>
    <hr style="border:0;border-top:1px solid var(--line);margin:12px 0">
    <div class="row">
      <span class="muted" style="font-size:12px">Trigger on</span>
      <span class="seg">
        <button id="mode-percent" type="button" onclick="setMode('percent')">Battery %</button>
        <button id="mode-runtime" type="button" onclick="setMode('runtime')">Runtime (min)</button>
      </span>
      <span id="mode-unsaved" class="pill warn" hidden>not saved yet</span>
    </div>
    <div class="row th-pair" id="pair-percent" style="margin-top:10px">
      <label>Guest % <input id="in-vm" type="number" min="1" max="100" style="width:64px"></label>
      <label>Host % <input id="in-host" type="number" min="1" max="100" style="width:64px"></label>
    </div>
    <div class="row th-pair" id="pair-runtime" style="margin-top:6px">
      <label>Guest min <input id="in-vm-min" type="number" min="1" max="240" style="width:64px"></label>
      <label>Host min <input id="in-host-min" type="number" min="1" max="240" style="width:64px"></label>
    </div>
    <div class="row" style="margin-top:10px">
      <button onclick="saveThresholds()">Save thresholds</button>
      <span style="flex:1"></span>
      <button class="danger" onclick="resetState()">Reset state → NORMAL</button>
    </div>
    <p class="muted" style="margin:8px 0 0 0;font-size:12px">
      Both pairs are kept; only the selected one triggers. Host must be lower
      than guest. In runtime mode, if the UPS reports no runtime the % pair is
      used for that minute. Reset only if you know the state file is stale.
    </p>
  </section>

  <section class="card" style="grid-column:1 / -1">
    <h2>Battery % vs runtime</h2>
    <p class="muted" style="margin:0 0 10px 0;font-size:12px">
      Now: <span id="cv-live">…</span>. The conversions below assume the
      current load and a roughly even drain, so treat them as estimates; runtime
      rises when guests shut down and load drops.
    </p>
    <div style="overflow-x:auto">
    <table id="cv-table">
      <thead><tr><th>Stage</th><th>Battery % trigger</th><th>≈ runtime then</th>
        <th>Runtime trigger</th><th>≈ battery % then</th></tr></thead>
      <tbody></tbody>
    </table>
    </div>
    <p id="hist-summary" style="margin:14px 0 6px 0;font-size:13px"></p>
    <div style="overflow-x:auto">
    <table id="hist-table">
      <thead><tr><th>Outage start</th><th>On battery</th><th>Battery</th>
        <th>Drain</th><th>Stages reached</th></tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </section>

  <section class="card" style="grid-column:1 / -1">
    <h2>Self-test</h2>
    <p class="muted" style="margin:0 0 10px 0;font-size:12px">
      Checks everything a power outage depends on: the UPS is readable, every
      enabled host accepts SSH and can run <code>qm</code>/<code>pct</code>, the
      email relay answers, and the cron worker is alive. Read-only; nothing is
      shut down. The daily run emails you on failure (every day until fixed) and
      once on recovery. Separately, the web app emails you if the cron worker
      stops for 10 minutes.
    </p>
    <div class="row" style="gap:12px">
      <span id="st-summary" class="muted">no self-test has run yet</span>
      <span style="flex:1"></span>
      <button id="st-run-btn" onclick="runSelftest()">Run self-test now</button>
    </div>
    <table id="st-table" style="margin-top:8px"><tbody></tbody></table>
    <hr style="border:0;border-top:1px solid var(--line);margin:12px 0">
    <div class="row" style="gap:16px">
      <label class="muted" style="font-size:13px;display:flex;align-items:center;gap:4px">
        <input id="st-enabled" type="checkbox"> Daily self-test + worker watchdog
      </label>
      <label class="muted" style="font-size:13px">Run daily at
        <input id="st-hour" type="number" min="0" max="23" style="width:60px">:00
        <span id="st-clock" style="font-size:12px"></span>
      </label>
      <label class="muted" style="font-size:13px;display:flex;align-items:center;gap:4px">
        <input id="st-success" type="checkbox"> Also email when everything passes
      </label>
      <button id="st-save-btn" onclick="saveSelftestConfig()">Save</button>
    </div>
    <p id="st-msg" class="muted" style="font-size:12px;margin:8px 0 0 0;min-height:1em"></p>
  </section>

  <section class="card" style="grid-column:1 / -1">
    <h2>UPS connection</h2>
    <p class="muted" style="margin:0 0 10px 0;font-size:12px">
      Edits <code>/etc/nut/ups.conf</code>, restarts NUT, and verifies the new
      settings work by polling <code>upsc</code>. If the poll fails the old
      config is automatically restored.
    </p>
    <div class="row" style="align-items:flex-end">
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">Name</span>
        <input id="ups-name" type="text" style="width:140px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">NMC IP / host</span>
        <input id="ups-host" type="text" style="width:160px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">SNMP community</span>
        <input id="ups-community" type="text" style="width:140px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">Poll freq (s)</span>
        <input id="ups-pollfreq" type="number" min="1" max="300" style="width:80px">
      </label>
      <span class="muted" style="font-size:12px;flex:1">
        driver=<span id="ups-driver">…</span>, snmp_version=<span id="ups-snmpv">…</span>
      </span>
      <button id="ups-save-btn" onclick="saveUpsConfig()">Save &amp; reload NUT</button>
    </div>
    <p id="ups-save-msg" class="muted" style="font-size:12px;margin:8px 0 0 0;min-height:1em"></p>
  </section>

  <section class="card" style="grid-column:1 / -1">
    <h2>Notifications (email)</h2>
    <p class="muted" style="margin:0 0 10px 0;font-size:12px">
      Email alerts fire from the cron worker when the listed events happen.
      Each event sends at most once per outage; on power restored, the bookkeeping
      resets. Use a "Send test email" to verify SMTP before relying on it.
    </p>
    <div class="row" style="align-items:flex-end;gap:10px;flex-wrap:wrap">
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">SMTP host</span>
        <input id="n-host" type="text" placeholder="192.168.0.10 or smtp.example.com" style="width:200px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">Port</span>
        <input id="n-port" type="number" min="1" max="65535" style="width:80px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">SMTP user (optional)</span>
        <input id="n-user" type="text" autocomplete="off" style="width:160px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">SMTP password (optional)</span>
        <input id="n-pass" type="password" autocomplete="new-password" style="width:160px">
      </label>
      <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:4px;margin-bottom:6px">
        <input id="n-tls" type="checkbox"> STARTTLS / TLS
      </label>
    </div>
    <div class="row" style="align-items:flex-end;gap:10px;margin-top:10px;flex-wrap:wrap">
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">From address</span>
        <input id="n-from" type="text" placeholder="alerts@example.com" style="width:240px">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px;flex:1;min-width:240px">
        <span class="muted" style="font-size:11px">To (comma-separated)</span>
        <input id="n-to" type="text" placeholder="you@example.com, oncall@example.com" style="width:100%">
      </label>
      <label style="display:flex;flex-direction:column;gap:2px">
        <span class="muted" style="font-size:11px">Warning at battery %</span>
        <input id="n-warn" type="number" min="1" max="100" style="width:90px">
      </label>
    </div>
    <div class="row" style="gap:18px;margin-top:14px;flex-wrap:wrap">
      <span class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.05em">Send email on:</span>
      <label class="muted" style="font-size:13px;display:flex;align-items:center;gap:4px">
        <input id="n-ev-warning" type="checkbox"> Warning threshold hit
      </label>
      <label class="muted" style="font-size:13px;display:flex;align-items:center;gap:4px">
        <input id="n-ev-guests" type="checkbox"> Guest shutdown stage
      </label>
      <label class="muted" style="font-size:13px;display:flex;align-items:center;gap:4px">
        <input id="n-ev-hosts" type="checkbox"> Host shutdown stage
      </label>
      <label class="muted" style="font-size:13px;display:flex;align-items:center;gap:4px">
        <input id="n-ev-restored" type="checkbox"> Power restored
      </label>
    </div>
    <div class="row" style="margin-top:14px">
      <button id="n-save-btn" onclick="saveNotifications()">Save</button>
      <button class="ghost" onclick="testNotificationEmail()">Send test email</button>
      <span style="flex:1"></span>
    </div>
    <p id="n-save-msg" class="muted" style="font-size:12px;margin:8px 0 0 0;min-height:1em"></p>
  </section>

  <section class="card" style="grid-column:1 / -1">
    <h2>Hosts</h2>
    <table id="hosts-table">
      <thead><tr><th>Enabled</th><th>IP</th><th>Label</th><th>Note</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
    <hr style="border:0;border-top:1px solid var(--line);margin:12px 0">
    <div class="row">
      <input id="new-ip" type="text" placeholder="10.0.0.x" style="width:140px">
      <input id="new-label" type="text" placeholder="label (e.g. pve-01)" style="width:160px">
      <input id="new-note" type="text" placeholder="note (optional)" style="flex:1;min-width:160px">
      <label class="muted" style="font-size:12px;display:flex;align-items:center;gap:4px">
        <input id="new-pushkey" type="checkbox"> push SSH key on add
      </label>
      <button onclick="addHost()">Add host</button>
      <button class="ghost" onclick="testAllSsh()">Test SSH on all</button>
    </div>
    <p class="muted" style="margin:8px 0 0 0;font-size:12px">
      "Push SSH key" runs <code>ssh-copy-id</code> against the host using a one-time
      root password you supply. The password is never stored or logged.
    </p>
  </section>

  <section class="card">
    <h2>Simulate</h2>
    <p class="muted" style="margin:0 0 8px 0;font-size:12px">
      Pretend the UPS is on battery at the given charge and show which action would fire.
      <b>Nothing is actually shut down.</b>
    </p>
    <div class="row">
      <label>Charge % <input id="sim-charge" type="number" value="55" min="0" max="100" style="width:70px"></label>
      <label>Runtime min <input id="sim-runtime" type="number" min="0" max="240" placeholder="auto" style="width:70px"
        title="Leave blank to estimate from the charge at the current load"></label>
      <button onclick="simulate()">Run simulation</button>
    </div>
    <pre id="sim-out" class="log" style="height:160px"></pre>
  </section>

  <section class="card">
    <h2>Recent log</h2>
    <pre id="log-tail" class="log"></pre>
  </section>
</div>

<div class="foot">Login credentials: <code>web.username/password</code> in <code>config.json</code>. API endpoints also accept HTTP Basic Auth.</div>

<div id="modal-backdrop" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;align-items:center;justify-content:center">
  <div style="background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:20px;min-width:360px;max-width:480px">
    <h3 id="modal-title" style="margin:0 0 8px 0">Push SSH key</h3>
    <p id="modal-sub" class="muted" style="margin:0 0 12px 0;font-size:13px"></p>
    <input id="modal-input" type="password" autocomplete="off" placeholder="root password"
           style="width:100%;background:#0c0e13;border:1px solid var(--line);border-radius:5px;padding:8px;color:var(--ink)">
    <p id="modal-msg" style="font-size:12px;color:var(--muted);margin:8px 0 0 0;min-height:1em"></p>
    <div class="row" style="justify-content:flex-end;margin-top:12px">
      <button class="ghost" onclick="modalCancel()">Cancel</button>
      <button id="modal-ok" onclick="modalOk()">Push key</button>
    </div>
  </div>
</div>

<script>
async function api(path, opts){
  const r = await fetch(path, opts);
  if(r.status === 401){ window.location = "/login"; throw new Error("auth required"); }
  if(!r.ok){ throw new Error("HTTP " + r.status); }
  return r.json();
}
function esc(s){
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}
async function editCell(el, ip, field){
  const original = el.textContent === "click to set label" || el.textContent === "click to add note"
                   ? "" : el.textContent;
  const input = document.createElement("input");
  input.type = "text";
  input.value = original;
  input.style.cssText = "width:100%;background:#0c0e13;border:1px solid var(--accent);border-radius:4px;padding:4px 6px;color:var(--ink);font:inherit";
  el.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const save = async () => {
    if(done) return;
    done = true;
    const newVal = input.value.trim();
    if(newVal === original){ refresh(); return; }
    const r = await fetch("/api/host/"+ip, {method:"PATCH",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({[field]: newVal})});
    if(!r.ok){ alert("edit failed: "+r.status+" "+await r.text()); }
    refresh();
  };
  input.addEventListener("blur", save);
  input.addEventListener("keydown", e => {
    if(e.key === "Enter"){ e.preventDefault(); save(); }
    if(e.key === "Escape"){ done = true; refresh(); }
  });
}
function fmtRuntime(s){
  if(!s) return "—";
  const m = Math.floor(s/60), ss = s%60;
  return `${m}m ${ss}s`;
}
async function refresh(){
  const s = await api("/api/status");
  document.getElementById("ups-name").textContent = s.ups_name;
  document.getElementById("ups-model").textContent = s.model;
  document.getElementById("ups-status").textContent = s.ups_status;
  document.getElementById("ups-charge").textContent = s.charge;
  document.getElementById("ups-runtime").textContent = fmtRuntime(s.runtime_s);
  const bar = document.getElementById("ups-bar");
  bar.firstElementChild.style.width = s.charge + "%";
  bar.className = "bar" + (s.charge<=s.thresholds.host_shutdown_pct?" crit":
                           s.charge<=s.thresholds.vm_shutdown_pct?" low":"");
  document.getElementById("state-text").textContent = s.state;
  lastStatus = s;
  const th = s.thresholds;
  const stageText = (pct, min) => th.mode === "runtime"
    ? `runtime ≤ ${min} min` + (s.effective_mode === "percent" && s.ups_ok
        ? ` (UPS reports no runtime now; ≤ ${pct}% used)` : "")
    : `battery ≤ ${pct}%`;
  document.getElementById("th-vm-text").textContent =
    stageText(th.vm_shutdown_pct, th.vm_shutdown_min) + " → shut down guests";
  document.getElementById("th-host-text").textContent =
    stageText(th.host_shutdown_pct, th.host_shutdown_min) + " → shut down hosts";
  const thFocus = ["in-vm","in-host","in-vm-min","in-host-min"].includes(
    document.activeElement && document.activeElement.id);
  if(!thFocus && !thDirty){
    document.getElementById("in-vm").value = th.vm_shutdown_pct;
    document.getElementById("in-host").value = th.host_shutdown_pct;
    document.getElementById("in-vm-min").value = th.vm_shutdown_min;
    document.getElementById("in-host-min").value = th.host_shutdown_min;
    pendingMode = th.mode;
  }
  renderMode();
  renderConversions();
  loadHistory();

  const upsPill = document.getElementById("ups-pill");
  upsPill.textContent = s.ups_ok ? s.ups_status : "UPS ERR";
  upsPill.className = "pill " + (!s.ups_ok?"bad":
                                 s.ups_status.includes("OL")?"ok":"warn");
  const statePill = document.getElementById("state-pill");
  statePill.textContent = s.state;
  statePill.className = "pill " + (s.state==="NORMAL"?"ok":
                                   s.state==="HOSTS_DOWN"?"bad":"warn");

  const wp = document.getElementById("worker-pill");
  const wa = s.worker_age_s;
  const wStale = wa === null || wa > s.worker_stale_s;
  wp.textContent = wa === null ? "worker: no heartbeat" :
                   "worker " + (wa < 120 ? wa + "s" : Math.floor(wa/60) + "m") + " ago";
  wp.className = "pill " + (wStale ? "bad" : "ok");
  const sp = document.getElementById("selftest-pill");
  sp.textContent = s.selftest_ok === true ? "self-test OK" :
                   s.selftest_ok === false ? "SELF-TEST FAILED" : "self-test: never run";
  sp.className = "pill " + (s.selftest_ok === true ? "ok" : s.selftest_ok === false ? "bad" : "warn");

  const tbody = document.querySelector("#hosts-table tbody");
  tbody.innerHTML = "";
  for(const h of s.hosts){
    const tr = document.createElement("tr");
    const ip = esc(h.ip);
    const label = esc(h.label||"");
    const note = esc(h.note||"");
    tr.innerHTML = `
      <td><input type="checkbox" ${h.enabled?"checked":""} onchange="toggle('${ip}', this.checked)"></td>
      <td><code>${ip}</code></td>
      <td><span class="editable" title="click to edit" onclick="editCell(this,'${ip}','label')">${label||"<span class='placeholder'>click to set label</span>"}</span></td>
      <td><span class="editable muted" title="click to edit" onclick="editCell(this,'${ip}','note')">${note||"<span class='placeholder'>click to add note</span>"}</span></td>
      <td class="row-actions">
        <button class="ghost" onclick="testOne('${ip}')">Test SSH</button>
        <button class="ghost" onclick="pushKey('${ip}','${label||ip}')">Push key</button>
        <button class="ghost" title="Forget saved SSH host key (use after reinstalling this host)" onclick="resetHostKey('${ip}','${label||ip}')">Reset host key</button>
        <button class="danger" onclick="removeHost('${ip}')">Remove</button>
      </td>`;
    tbody.appendChild(tr);
  }

  document.getElementById("log-tail").textContent = s.log_tail.join("\n");

  // populate UPS connection form (only if user hasn't typed into it)
  const focusId = document.activeElement && document.activeElement.id || "";
  const isUpsFocus = focusId.startsWith("ups-") && document.activeElement.tagName === "INPUT";
  const isNFocus = focusId.startsWith("n-") && document.activeElement.tagName === "INPUT";
  const isStFocus = focusId.startsWith("st-") && document.activeElement.tagName === "INPUT";
  try {
    const st = await api("/api/selftest");
    renderSelftest(st.last);
    document.getElementById("st-clock").textContent =
      `(server clock ${st.server_tz}, now ${st.server_time})`;
    if(!isStFocus){
      document.getElementById("st-enabled").checked = !!st.config.enabled;
      document.getElementById("st-hour").value = st.config.hour;
      document.getElementById("st-success").checked = !!st.config.email_on_success;
    }
  } catch(e){ /* ignore */ }
  if(!isUpsFocus){
    try {
      const u = await api("/api/ups/config");
      document.getElementById("ups-name").value = u.name || "";
      document.getElementById("ups-host").value = u.host || "";
      document.getElementById("ups-community").value = u.community || "";
      document.getElementById("ups-pollfreq").value = u.pollfreq || "";
      document.getElementById("ups-driver").textContent = u.driver || "?";
      document.getElementById("ups-snmpv").textContent = u.snmp_version || "?";
    } catch(e){ /* ignore */ }
  }
  if(!isNFocus){
    try {
      const n = await api("/api/notifications/config");
      document.getElementById("n-host").value = n.smtp_host || "";
      document.getElementById("n-port").value = n.smtp_port || 25;
      document.getElementById("n-user").value = n.smtp_user || "";
      document.getElementById("n-pass").value = n.smtp_password || "";
      document.getElementById("n-tls").checked = !!n.smtp_use_tls;
      document.getElementById("n-from").value = n.from_address || "";
      document.getElementById("n-to").value = (n.to_addresses||[]).join(", ");
      document.getElementById("n-warn").value = n.warning_pct || 80;
      document.getElementById("n-ev-warning").checked = !!n.events.warning;
      document.getElementById("n-ev-guests").checked = !!n.events.guests_shutdown;
      document.getElementById("n-ev-hosts").checked = !!n.events.hosts_shutdown;
      document.getElementById("n-ev-restored").checked = !!n.events.power_restored;
    } catch(e){ /* ignore */ }
  }
}

function renderSelftest(r){
  const sum = document.getElementById("st-summary");
  const tbody = document.querySelector("#st-table tbody");
  if(!r){ sum.textContent = "no self-test has run yet"; tbody.innerHTML = ""; return; }
  sum.innerHTML = `<span class="pill ${r.ok?"ok":"bad"}">${r.ok?"PASSED":"FAILED"}</span>
    <span class="muted" style="margin-left:6px">${esc(r.time)} (${esc(r.trigger)})</span>`;
  tbody.innerHTML = r.checks.map(c => `<tr>
      <td style="width:28px">${c.ok ? "<span style='color:var(--ok)'>✓</span>"
                                    : "<span style='color:var(--bad)'>✗</span>"}</td>
      <td style="width:160px">${esc(c.name)}</td>
      <td class="${c.ok?"muted":""}">${esc(c.msg)}</td></tr>`).join("");
}

async function runSelftest(){
  const btn = document.getElementById("st-run-btn");
  const msg = document.getElementById("st-msg");
  btn.disabled = true; msg.textContent = "running self-test …"; msg.style.color = "var(--muted)";
  try {
    const r = await api("/api/selftest/run", {method:"POST"});
    renderSelftest(r);
    msg.textContent = r.ok ? "all checks passed" : "some checks failed (see above)";
    msg.style.color = r.ok ? "var(--ok)" : "var(--bad)";
  } catch(e){
    msg.textContent = "self-test request failed: " + e.message; msg.style.color = "var(--bad)";
  }
  btn.disabled = false;
  refresh();
}

async function saveSelftestConfig(){
  const msg = document.getElementById("st-msg");
  const body = {
    enabled: document.getElementById("st-enabled").checked,
    hour: parseInt(document.getElementById("st-hour").value),
    email_on_success: document.getElementById("st-success").checked,
  };
  const r = await fetch("/api/selftest/config", {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const data = await r.json().catch(()=>({ok:false,msg:"bad response"}));
  msg.textContent = (data.ok ? "saved" : "save failed: ") + (data.msg || "");
  msg.style.color = data.ok ? "var(--ok)" : "var(--bad)";
}

async function saveNotifications(){
  const body = {
    smtp_host: document.getElementById("n-host").value.trim(),
    smtp_port: parseInt(document.getElementById("n-port").value) || 25,
    smtp_user: document.getElementById("n-user").value.trim(),
    smtp_password: document.getElementById("n-pass").value,
    smtp_use_tls: document.getElementById("n-tls").checked,
    from_address: document.getElementById("n-from").value.trim(),
    to_addresses: document.getElementById("n-to").value,
    warning_pct: parseInt(document.getElementById("n-warn").value) || 80,
    events: {
      warning: document.getElementById("n-ev-warning").checked,
      guests_shutdown: document.getElementById("n-ev-guests").checked,
      hosts_shutdown: document.getElementById("n-ev-hosts").checked,
      power_restored: document.getElementById("n-ev-restored").checked,
    },
  };
  const btn = document.getElementById("n-save-btn");
  const msg = document.getElementById("n-save-msg");
  btn.disabled = true; msg.textContent = "saving …"; msg.style.color = "var(--muted)";
  const r = await fetch("/api/notifications/config", {method:"POST",
    headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const data = await r.json().catch(()=>({ok:false,msg:"bad response"}));
  btn.disabled = false;
  msg.textContent = (data.ok ? "saved" : "save failed: ") + (data.msg || "");
  msg.style.color = data.ok ? "var(--ok)" : "var(--bad)";
}

async function testNotificationEmail(){
  const msg = document.getElementById("n-save-msg");
  msg.textContent = "sending test email …"; msg.style.color = "var(--muted)";
  const r = await fetch("/api/notifications/test", {method:"POST"});
  const data = await r.json().catch(()=>({ok:false,msg:"bad response"}));
  msg.textContent = (data.ok ? "test email sent: " : "test email failed: ") + (data.msg || "");
  msg.style.color = data.ok ? "var(--ok)" : "var(--bad)";
}

async function saveUpsConfig(){
  const body = {
    name: document.getElementById("ups-name").value.trim(),
    host: document.getElementById("ups-host").value.trim(),
    community: document.getElementById("ups-community").value.trim(),
    pollfreq: parseInt(document.getElementById("ups-pollfreq").value),
  };
  if(!confirm(`Save UPS settings and reload NUT?\n\nname=${body.name}\nhost=${body.host}\ncommunity=${body.community}\npollfreq=${body.pollfreq}s\n\nIf the new settings don't work, the old config will be restored.`)) return;
  const btn = document.getElementById("ups-save-btn");
  const msg = document.getElementById("ups-save-msg");
  btn.disabled = true;
  msg.textContent = "saving, restarting NUT, verifying… (~10s)";
  msg.style.color = "var(--muted)";
  const r = await fetch("/api/ups/config", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(()=>({ok:false,msg:"bad response"}));
  btn.disabled = false;
  if(data.ok){
    msg.textContent = "✓ " + (data.msg || "applied");
    msg.style.color = "var(--ok)";
  } else {
    msg.textContent = "✗ " + (data.msg || "failed");
    msg.style.color = "var(--bad)";
  }
  refresh();
}
async function toggle(ip, enabled){
  const r = await fetch("/api/host/"+ip, {method:"PATCH",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({enabled})});
  if(!r.ok){ alert("toggle failed: "+r.status+" "+await r.text()); }
  refresh();
}
async function addHost(){
  const ip = document.getElementById("new-ip").value.trim();
  const label = document.getElementById("new-label").value.trim();
  const note = document.getElementById("new-note").value.trim();
  const pushOnAdd = document.getElementById("new-pushkey").checked;
  if(!ip){ alert("ip required"); return; }
  const r = await fetch("/api/host", {method:"POST", headers:{"Content-Type":"application/json"},
                                       body: JSON.stringify({ip,label,note,enabled:true})});
  if(!r.ok){ alert("add failed: "+await r.text()); return; }
  document.getElementById("new-ip").value="";
  document.getElementById("new-label").value="";
  document.getElementById("new-note").value="";
  document.getElementById("new-pushkey").checked=false;
  await refresh();
  if(pushOnAdd){ pushKey(ip, label||ip); }
}

// --- modal ---
let _modalResolve = null;
function showModal(title, sub){
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-sub").textContent = sub || "";
  document.getElementById("modal-input").value = "";
  document.getElementById("modal-msg").textContent = "";
  document.getElementById("modal-ok").disabled = false;
  document.getElementById("modal-backdrop").style.display = "flex";
  setTimeout(()=>document.getElementById("modal-input").focus(), 50);
  return new Promise(res => { _modalResolve = res; });
}
function modalOk(){
  const v = document.getElementById("modal-input").value;
  if(_modalResolve){ const r=_modalResolve; _modalResolve=null; r(v); }
}
function modalCancel(){
  document.getElementById("modal-backdrop").style.display = "none";
  if(_modalResolve){ const r=_modalResolve; _modalResolve=null; r(null); }
}
function modalSetMsg(text, isError){
  const el = document.getElementById("modal-msg");
  el.textContent = text;
  el.style.color = isError ? "var(--bad)" : "var(--muted)";
}
document.addEventListener("keydown", e => {
  if(document.getElementById("modal-backdrop").style.display !== "flex") return;
  if(e.key === "Escape") modalCancel();
  if(e.key === "Enter") modalOk();
});

async function pushKey(ip, label){
  while(true){
    const pw = await showModal(`Push SSH key to ${label}`,
      `Runs ssh-copy-id against ${ip}. Enter the host's root password (used once, not stored).`);
    if(pw === null){ document.getElementById("modal-backdrop").style.display = "none"; return; }
    if(!pw){ modalSetMsg("password required", true); continue; }
    document.getElementById("modal-ok").disabled = true;
    modalSetMsg("pushing key …", false);
    const r = await fetch(`/api/host/${ip}/push-key`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({password: pw})
    });
    if(r.status === 401){
      modalSetMsg("auth required — close and retry, browser will prompt for nutserver login", true);
      document.getElementById("modal-ok").disabled = false;
      return;
    }
    const data = await r.json().catch(()=>({ok:false,msg:"bad response"}));
    if(data.ok){
      modalSetMsg("✓ " + (data.msg || "key installed"), false);
      setTimeout(()=>{ document.getElementById("modal-backdrop").style.display="none"; refresh(); }, 900);
      return;
    }
    modalSetMsg("✗ " + (data.msg || "failed"), true);
    document.getElementById("modal-ok").disabled = false;
  }
}
async function resetHostKey(ip, label){
  if(!confirm("Forget the saved SSH host key for "+label+" ("+ip+")?\n\nOnly do this if you reinstalled the host. The next SSH connection will trust whatever key it presents.")) return;
  const r = await fetch("/api/host/"+ip+"/reset-hostkey", {method:"POST"});
  if(r.status === 401){ window.location = "/login"; return; }
  const d = await r.json().catch(()=>({ok:false,msg:"bad response"}));
  if(!d.ok){ alert("reset failed: "+d.msg); return; }
  alert(label+": "+d.msg+(d.fingerprint ? "\n\nHost's current fingerprint:\n"+d.fingerprint+"\n\nNow click Test SSH (or Push key on a fresh install)." : "\n\n(host not reachable to read its fingerprint)"));
}
async function removeHost(ip){
  if(!confirm("Remove "+ip+" from config?")) return;
  const r = await fetch("/api/host/"+ip, {method:"DELETE"});
  if(!r.ok){ alert("remove failed: "+r.status+" "+await r.text()); return; }
  refresh();
}
let lastStatus = null, pendingMode = null, thDirty = false;
const num = id => parseInt(document.getElementById(id).value);

function setMode(m){ pendingMode = m; thDirty = true; renderMode(); renderConversions(); }

function renderMode(){
  if(!lastStatus) return;
  const m = pendingMode || lastStatus.thresholds.mode;
  document.getElementById("mode-percent").className = m === "percent" ? "on" : "";
  document.getElementById("mode-runtime").className = m === "runtime" ? "on" : "";
  document.getElementById("pair-percent").classList.toggle("off", m !== "percent");
  document.getElementById("pair-runtime").classList.toggle("off", m !== "runtime");
  document.getElementById("mode-unsaved").hidden = m === lastStatus.thresholds.mode;
}

// Linear estimate at the current load: runtime per % of charge.
function secsPerPct(){
  const s = lastStatus;
  return (s && s.ups_ok && s.charge > 0 && s.runtime_s > 0) ? s.runtime_s / s.charge : null;
}
function fmtMin(sec){ return sec == null ? "—" : (sec < 60 ? "<1 min" : Math.round(sec/60) + " min"); }

function renderConversions(){
  const s = lastStatus; if(!s) return;
  const spp = secsPerPct();
  const m = pendingMode || s.thresholds.mode;
  const load = s.load_pct != null ? `, load ${s.load_pct}%` : "";
  const watts = s.realpower_w != null ? ` (${s.realpower_w} W)` : "";
  document.getElementById("cv-live").textContent = s.ups_ok
    ? `battery ${s.charge}%, runtime ${fmtRuntime(s.runtime_s)}${load}${watts}`
    : "UPS unreadable";
  const pctOf = mins => spp ? Math.min(100, Math.round(mins * 60 / spp)) + "%" : "—";
  // Read the inputs so the table previews unsaved edits too.
  const rows = [
    ["Guest shutdown", num("in-vm"), num("in-vm-min")],
    ["Host shutdown",  num("in-host"), num("in-host-min")],
  ];
  const a = c => c ? "active" : "dim";
  document.querySelector("#cv-table tbody").innerHTML = rows.map(([name, pct, min]) => `<tr>
      <td>${name}</td>
      <td class="${a(m==="percent")}">≤ ${isNaN(pct)?"?":pct}%</td>
      <td class="${a(m==="percent")}">≈ ${spp && !isNaN(pct) ? fmtMin(pct*spp) : "—"}</td>
      <td class="${a(m==="runtime")}">≤ ${isNaN(min)?"?":min} min</td>
      <td class="${a(m==="runtime")}">≈ ${isNaN(min) ? "—" : pctOf(min)}</td></tr>`).join("");
}

async function loadHistory(){
  let h; try { h = await api("/api/stats"); } catch(e){ return; }
  const sum = document.getElementById("hist-summary");
  const r = h.median_drain_pct_per_min;
  const th = lastStatus ? lastStatus.thresholds : null;
  if(r && th){
    const from = p => Math.round((100 - p) / r);
    sum.innerHTML = `<b>In real outages your battery drained ≈ ${r}%/min</b>
      <span class="muted">(median of ${h.outages_with_rate} outage${h.outages_with_rate>1?"s":""} long enough to measure).
      At that rate, starting full: guest stage (${th.vm_shutdown_pct}%) after ≈ ${from(th.vm_shutdown_pct)} min,
      host stage (${th.host_shutdown_pct}%) after ≈ ${from(th.host_shutdown_pct)} min,
      empty after ≈ ${Math.round(100 / r)} min.</span>`;
  } else {
    sum.innerHTML = `<span class="muted">No outage long enough to measure a drain rate yet.</span>`;
  }
  document.querySelector("#hist-table tbody").innerHTML = h.outages.length ? h.outages.map(o => `<tr>
      <td>${esc(o.start)}</td>
      <td>${o.duration_min < 1 ? "<1 min (blip)" : esc(o.duration_min) + " min"}</td>
      <td>${o.charge_start}% → ${o.charge_low}%</td>
      <td>${o.drain_pct_per_min ? o.drain_pct_per_min + "%/min" : "—"}</td>
      <td class="muted">${o.stages.length ? esc(o.stages.join(", ")) : "none"}${o.restored ? "" : " (no restore logged)"}</td>
    </tr>`).join("") : `<tr><td colspan="5" class="muted">no outages in the log</td></tr>`;
}

async function saveThresholds(){
  const body = {
    mode: pendingMode || (lastStatus && lastStatus.thresholds.mode) || "percent",
    vm_shutdown_pct: num("in-vm"), host_shutdown_pct: num("in-host"),
    vm_shutdown_min: num("in-vm-min"), host_shutdown_min: num("in-host-min"),
  };
  const r = await fetch("/api/thresholds", {method:"POST", headers:{"Content-Type":"application/json"},
                                             body: JSON.stringify(body)});
  if(!r.ok){ alert("save failed: "+await r.text()); return; }
  thDirty = false;
  refresh();
}
document.addEventListener("input", e => {
  if(["in-vm","in-host","in-vm-min","in-host-min"].includes(e.target.id)){ thDirty = true; renderConversions(); }
});
async function resetState(){
  if(!confirm("Reset state to NORMAL? Do this only if you're sure the state file is stale.")) return;
  const r = await fetch("/api/state/reset", {method:"POST"});
  if(!r.ok){ alert("reset failed: "+r.status+" "+await r.text()); return; }
  refresh();
}
async function simulate(){
  const charge = parseInt(document.getElementById("sim-charge").value);
  let runtime_min = parseFloat(document.getElementById("sim-runtime").value);
  let est = "";
  if(isNaN(runtime_min)){
    const spp = secsPerPct();
    runtime_min = spp ? Math.round(charge * spp / 6) / 10 : null;
    est = " (estimated from charge at current load)";
  }
  const r = await api("/api/simulate", {method:"POST", headers:{"Content-Type":"application/json"},
                                         body: JSON.stringify({charge, runtime_min, on_battery:true})});
  let txt = `status=${r.status} charge=${r.charge}% runtime=${r.runtime_s==null?"unknown":fmtRuntime(r.runtime_s)}${est}\n`;
  txt += `trigger mode used: ${r.mode_used}   current_state=${r.state}\n`;
  txt += `decision: ${r.action}\n`;
  if(r.detail && r.detail.length){
    for(const d of r.detail){ txt += "  " + JSON.stringify(d) + "\n"; }
  } else {
    txt += "  (no shutdown action would fire)\n";
  }
  document.getElementById("sim-out").textContent = txt;
}
async function testOne(ip){
  const r = await api("/api/test-ssh", {method:"POST", headers:{"Content-Type":"application/json"},
                                         body: JSON.stringify({ip})});
  const res = r.results[0];
  alert(`${res.ip} (${res.label}): ${res.ok?"OK":"FAIL"} — ${res.msg}`);
}
async function testAllSsh(){
  const r = await api("/api/test-ssh", {method:"POST", headers:{"Content-Type":"application/json"},
                                         body: JSON.stringify({})});
  const lines = r.results.map(x=>`[${x.ok?"OK  ":"FAIL"}] ${x.ip} (${x.label}): ${x.msg}`);
  alert(lines.join("\n"));
}

refresh();
setInterval(refresh, 15000);
</script>
</body></html>
"""


LOGIN_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>nutshutdown — log in</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--ink:#e6e8ec;--muted:#8a93a6;
      --accent:#6aa9ff;--bad:#ef5d5d;--line:#262a33;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,Segoe UI,Inter,sans-serif;background:var(--bg);color:var(--ink);
     display:flex;align-items:center;justify-content:center;min-height:100vh}
form{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:28px;min-width:320px;max-width:380px}
h1{margin:0 0 4px;font-size:18px;font-weight:600}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px}
label{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:10px 0 4px}
input{width:100%;background:#0c0e13;border:1px solid var(--line);border-radius:5px;
      padding:9px 10px;color:var(--ink);font:inherit}
input:focus{outline:0;border-color:var(--accent)}
button{margin-top:18px;background:var(--accent);color:#03142e;border:0;border-radius:5px;
       padding:10px 12px;cursor:pointer;font-weight:600;width:100%;font:inherit}
.err{color:var(--bad);font-size:12px;margin:10px 0 0;min-height:1em}
.foot{color:var(--muted);font-size:11px;margin-top:14px;text-align:center}
</style></head><body>
<form method="POST" action="/login{% if request.args.get('next') %}?next={{ request.args.get('next') }}{% endif %}">
  <h1>nutshutdown</h1>
  <p class="sub">UPS-triggered graceful shutdown orchestrator</p>
  <label for="username">Username</label>
  <input id="username" name="username" autocomplete="username" autofocus>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password">
  <button type="submit">Log in</button>
  <div class="err">{% if error %}{{ error }}{% endif %}</div>
  <div class="foot">credentials live in <code>config.json</code> → <code>web.*</code></div>
</form>
</body></html>
"""


if __name__ == "__main__":
    cfg = pmlib.load_config()
    web = cfg.get("web", {})
    threading.Thread(target=_worker_watchdog, name="worker-watchdog",
                     daemon=True).start()
    app.run(host=web.get("bind", "0.0.0.0"), port=int(web.get("port", 8080)),
            debug=False)
