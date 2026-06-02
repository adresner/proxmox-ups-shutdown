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
        "model": ups.get("device.model", "?"),
        "state": pmlib.read_state(cfg),
        "thresholds": cfg["thresholds"],
        "ups_name": cfg["ups_name"],
        "hosts": [h for h in cfg["hosts"]],
        "log_tail": pmlib.tail_log(cfg, lines=30),
    })


@app.route("/api/simulate", methods=["POST"])
@requires_auth
def api_simulate():
    cfg = pmlib.load_config()
    data = request.get_json(force=True, silent=True) or {}
    charge = int(data.get("charge", 100))
    force_ob = bool(data.get("on_battery", True))
    status = "OB" if force_ob else "OL"
    state = pmlib.read_state(cfg)
    action = pmlib.decide_action(cfg, status, charge, state)
    detail = []
    if action == "shutdown_guests":
        detail = pmlib.shutdown_guests(cfg, dry_run=True)
    elif action == "shutdown_hosts":
        detail = pmlib.shutdown_hosts(cfg, dry_run=True)
    return jsonify({"status": status, "charge": charge, "state": state,
                    "action": action, "detail": detail})


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
    return jsonify({"ok": True})


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
    try:
        vm = int(data["vm_shutdown_pct"])
        host = int(data["host_shutdown_pct"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "vm_shutdown_pct + host_shutdown_pct (int) required"}), 400
    if not (0 < host < vm <= 100):
        return jsonify({"error": "require 0 < host < vm <= 100"}), 400
    cfg["thresholds"]["vm_shutdown_pct"] = vm
    cfg["thresholds"]["host_shutdown_pct"] = host
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
</style></head><body>
<header>
  <h1>nutserver — Proxmox graceful-shutdown orchestrator</h1>
  <span id="ups-pill" class="pill">…</span>
  <span id="state-pill" class="pill warn">…</span>
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
      <b>VM stage</b><span>battery ≤ <span id="th-vm">…</span>% → shut down guests</span>
      <b>Host stage</b><span>battery ≤ <span id="th-host">…</span>% → shut down hosts</span>
    </div>
    <hr style="border:0;border-top:1px solid var(--line);margin:12px 0">
    <div class="row">
      <label>VM% <input id="in-vm" type="number" min="1" max="100" style="width:70px"></label>
      <label>Host% <input id="in-host" type="number" min="1" max="100" style="width:70px"></label>
      <button onclick="saveThresholds()">Save thresholds</button>
      <span style="flex:1"></span>
      <button class="danger" onclick="resetState()">Reset state → NORMAL</button>
    </div>
    <p class="muted" style="margin:8px 0 0 0;font-size:12px">
      Host % must be lower than VM %. Reset only if you know the state file is stale.
    </p>
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
        <input id="n-host" type="text" placeholder="smtp.gst.co.th" style="width:200px">
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
        <input id="n-from" type="text" placeholder="notify@gst.co.th" style="width:240px">
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
      <input id="new-ip" type="text" placeholder="192.168.1.x" style="width:140px">
      <input id="new-label" type="text" placeholder="label (e.g. hq-pv5)" style="width:160px">
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
      <label>Charge % <input id="sim-charge" type="number" value="55" min="0" max="100" style="width:80px"></label>
      <button onclick="simulate()">Run simulation</button>
    </div>
    <pre id="sim-out" class="log" style="height:160px"></pre>
  </section>

  <section class="card">
    <h2>Recent log</h2>
    <pre id="log-tail" class="log"></pre>
  </section>
</div>

<div class="foot">Edits prompt for HTTP basic auth (see <code>web.username/password</code> in <code>config.json</code>).</div>

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
  document.getElementById("th-vm").textContent = s.thresholds.vm_shutdown_pct;
  document.getElementById("th-host").textContent = s.thresholds.host_shutdown_pct;
  document.getElementById("in-vm").value = s.thresholds.vm_shutdown_pct;
  document.getElementById("in-host").value = s.thresholds.host_shutdown_pct;

  const upsPill = document.getElementById("ups-pill");
  upsPill.textContent = s.ups_ok ? s.ups_status : "UPS ERR";
  upsPill.className = "pill " + (!s.ups_ok?"bad":
                                 s.ups_status.includes("OL")?"ok":"warn");
  const statePill = document.getElementById("state-pill");
  statePill.textContent = s.state;
  statePill.className = "pill " + (s.state==="NORMAL"?"ok":
                                   s.state==="HOSTS_DOWN"?"bad":"warn");

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
        <button class="danger" onclick="removeHost('${ip}')">Remove</button>
      </td>`;
    tbody.appendChild(tr);
  }

  document.getElementById("log-tail").textContent = s.log_tail.join("\n");

  // populate UPS connection form (only if user hasn't typed into it)
  const focusId = document.activeElement && document.activeElement.id || "";
  const isUpsFocus = focusId.startsWith("ups-") && document.activeElement.tagName === "INPUT";
  const isNFocus = focusId.startsWith("n-") && document.activeElement.tagName === "INPUT";
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
async function removeHost(ip){
  if(!confirm("Remove "+ip+" from config?")) return;
  const r = await fetch("/api/host/"+ip, {method:"DELETE"});
  if(!r.ok){ alert("remove failed: "+r.status+" "+await r.text()); return; }
  refresh();
}
async function saveThresholds(){
  const vm = parseInt(document.getElementById("in-vm").value);
  const host = parseInt(document.getElementById("in-host").value);
  const r = await fetch("/api/thresholds", {method:"POST", headers:{"Content-Type":"application/json"},
                                             body: JSON.stringify({vm_shutdown_pct:vm,host_shutdown_pct:host})});
  if(!r.ok){ alert("save failed: "+await r.text()); return; }
  refresh();
}
async function resetState(){
  if(!confirm("Reset state to NORMAL? Do this only if you're sure the state file is stale.")) return;
  const r = await fetch("/api/state/reset", {method:"POST"});
  if(!r.ok){ alert("reset failed: "+r.status+" "+await r.text()); return; }
  refresh();
}
async function simulate(){
  const charge = parseInt(document.getElementById("sim-charge").value);
  const r = await api("/api/simulate", {method:"POST", headers:{"Content-Type":"application/json"},
                                         body: JSON.stringify({charge,on_battery:true})});
  let txt = `status=${r.status} charge=${r.charge}% current_state=${r.state}\n`;
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
    app.run(host=web.get("bind", "0.0.0.0"), port=int(web.get("port", 8080)),
            debug=False)
