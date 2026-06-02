# Changelog

All notable changes to this project are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## [0.3.0] — Email notifications

### Added

- **Email alerts on UPS state transitions.** Four events, individually
  enable/disable-able from the GUI:
  - **Warning** — battery on UPS drops to a configurable `warning_pct`
    (default 80%) while still above the guest-shutdown threshold.
  - **Guest shutdown stage fired** — battery hit `vm_shutdown_pct`, the
    worker SSH'd `qm` / `pct shutdown` to every enabled host.
  - **Host shutdown stage fired** — battery hit `host_shutdown_pct`, the
    worker SSH'd `shutdown -h now`.
  - **Power restored** — UPS reports OL, state machine resetting.
- **"Notifications" card in the dashboard** with SMTP host/port/user/password
  (all optional), STARTTLS toggle, From, comma-separated To list, warning %,
  and four event checkboxes. Plus a "Send test email" button so you can
  verify SMTP before relying on it.
- **Endpoints**: `GET /api/notifications/config`, `POST
  /api/notifications/config`, `POST /api/notifications/test` (all
  authenticated).
- **`notify_event()` and supporting helpers in `pmlib.py`** — gated by the
  per-event enable flag in config and the `sent` list in the state file, so
  each event fires at most once per outage no matter how many times the
  cron worker tick.

### Changed

- **State file format extended** from plain text to a JSON blob:
  `{"state": "NORMAL"|"GUESTS_DOWN"|"HOSTS_DOWN", "sent": ["warning", ...]}`.
  Legacy plain-text state files are read transparently and rewritten as JSON
  on the next state change — no manual migration. Reset to NORMAL clears
  the `sent` list so the next outage gets a fresh round of emails.
- `power_monitor.py` now calls `notify_event()` at every relevant point:
  warning crossing, guest shutdown, host shutdown, and power restored.
- `DRY_RUN=1` for the worker also dry-runs email sends (logged but not
  actually transmitted).

## [0.2.0] — Session-based login form (replaces HTTP Basic Auth)

### Changed

- **Replaced HTTP Basic Auth with a real login form** at `/login`. The browser
  Basic Auth dialog was unreliable: it wouldn't appear after a previously
  canceled dialog, stayed wedged in Chrome's HTTP auth cache even after
  clearing site data, and required quitting the entire browser to clear stale
  credentials after a password rotation.
- Sessions are signed cookies (Flask's built-in session, HttpOnly, SameSite=Lax,
  30-day lifetime). The signing key is auto-generated on first run and stored
  in `.session_secret` next to `webapp.py` (mode 0600).
- A "Log out" button is in the dashboard header.

### Added

- `GET /login` — login form page.
- `POST /login` — validates credentials against `config.json`'s `web.username`
  + `web.password`, sets a session cookie, redirects to the `next` URL (or `/`).
- `GET|POST /logout` — clears the session, redirects to `/login`.

### Kept

- **HTTP Basic Auth still works on API endpoints** as a fallback for
  curl/scripts/cron, so existing automation doesn't break — only the browser
  flow uses sessions.
- All previously-public read endpoints (`/api/status`, `/api/simulate`,
  `/api/test-ssh`, `GET /api/ups/config`) now also require auth, for
  consistency now that the dashboard always requires login.

## [0.1.2] — Inline-editable host labels and notes

### Added

- **Click-to-edit on Label and Note columns** in the hosts table. Click the
  text → it becomes an input → press Enter or click away to save → Escape
  cancels. Empty cells show a faint "click to set label" / "click to add
  note" hint so the affordance is obvious. The PATCH backend already
  supported these fields; this just exposes them in the UI.

### Fixed

- **XSS hardening on the hosts table.** Host IP/label/note values are now
  HTML-escaped before being inlined into the table row template, so a
  label like `<script>` won't execute. (Previously the values came from
  config.json which is owner-written, so this was theoretical rather than
  exploitable — but worth fixing anyway.)

## [0.1.1] — Auth fix

### Fixed

- **Mutating buttons (Remove, Toggle, Reset State) silently failed.** Browsers
  don't show the Basic Auth login dialog for `fetch()` requests — only for
  top-level navigations — so without first authenticating via a navigation
  the UI was sending DELETE/PATCH/POST without credentials and the server
  was rejecting them with 401. The buttons returned no visible feedback,
  making them appear broken.
- **The index page now requires auth.** Loading the dashboard prompts for
  login once per browser session via the standard browser dialog, after
  which all mutating buttons work.
- Added explicit error-handling to `toggle`, `removeHost`, and `resetState`
  so any future non-2xx response is surfaced as an alert instead of being
  silently swallowed.

## [0.1.0] — Initial release

First public version. Everything works end-to-end against an APC Smart-UPS
SRT 5000 with an AP9641 network management card and a small Proxmox VE cluster.

### Added

- **Cron worker** (`power_monitor.py`) — every minute, reads UPS state via
  `upsc` and decides whether to do nothing, shut down guests, shut down hosts,
  or reset the state machine. Honors `DRY_RUN=1` for safe testing.
- **State machine** with three values (`NORMAL` / `GUESTS_DOWN` / `HOSTS_DOWN`)
  in `/var/run/power_state`, so the per-minute cron can't double-fire.
- **CLI** (`manage.py`) covering add/remove/enable/disable hosts, set
  thresholds, simulate at a fake battery percentage, test SSH connectivity,
  push SSH keys, reset the state file, and dump the full config.
- **Web UI** (`webapp.py`, Flask, single-file with inline HTML/CSS/JS) with:
  - Live UPS panel (status pill, charge bar with warn/crit colors, runtime).
  - State machine + threshold editor + reset button.
  - Host table with toggle, "Test SSH", "Push key" (via `sshpass` server-side),
    and "Remove" actions; add-host row with optional key-push on add.
  - **UPS connection editor** — name, NMC IP, SNMP community, poll frequency.
    Edits rewrite `/etc/nut/ups.conf`, restart NUT, and verify the new config
    by polling `upsc`. If the verify fails the previous config is restored
    automatically. Typical happy-path latency ≈ 1 s; rollback path ≈ 30 s.
  - "Simulate" panel that runs the full decision logic at a chosen charge
    and prints the SSH commands that would fire — no real shutdowns.
  - Recent log tail.
- **systemd unit** for the web app and a **`TimeoutStopSec=10` drop-in** for
  `nut-driver@.service` so a stuck SNMP driver (e.g. polling an unreachable
  UPS) can't hang a web-driven config edit.
- **Validate-and-rollback** on every UPS connection save, so a wrong NMC IP
  or community string can't take NUT offline from the browser.
- HTTP Basic Auth on all mutating endpoints.
- `install.sh` for Debian/Ubuntu/Proxmox-LXC, plus templated NUT config
  samples in `examples/`.
- Documentation: README, this CHANGELOG, MIT LICENSE.

### Documentation

- "Topology" section in the README explaining that the orchestrator should
  not be installed on a host that's also in its own shutdown list, with the
  recommended "small utility host alongside the workload hosts" pattern.

### Known limitations

- One UPS, one NUT section.
- SNMP v1/v2c only via the UI (v3 works if you edit `ups.conf` directly).
- No HTTPS in the bundled web app — put it behind a reverse proxy if you
  expose it beyond a trusted LAN.
- Targets Proxmox VE specifically (`qm` / `pct` shutdown commands).
