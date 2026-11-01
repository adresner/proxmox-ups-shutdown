# Changelog

All notable changes to this project are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

## [0.5.0] — Trigger on battery % or runtime, with drain stats

### Added

- **Runtime-based thresholds.** `thresholds.mode` = `percent` (default,
  unchanged behaviour) or `runtime`. Runtime mode uses `vm_shutdown_min` /
  `host_shutdown_min`, compared with the UPS's runtime estimate. Both pairs
  are always stored, so modes can be flipped freely. If the UPS reports no
  runtime in a given minute, that minute falls back to the % pair.
- **"Trigger on" switch** in the State machine card, with both threshold
  pairs editable and an "not saved yet" marker until you save.
- **"Battery % vs runtime" card**: live charge / runtime / load / watts; a
  table converting each % threshold to minutes at the current load and each
  minute threshold to %, previewing unsaved edits; and past outages parsed
  from the log (duration, charge drop, drain %/min), with the median drain
  turned into "guest stage after ≈ N min, host stage after ≈ N min, empty
  after ≈ N min" from a full battery.
- `GET /api/stats`; `/api/thresholds` accepts `mode` and the minute
  thresholds; `/api/simulate` accepts `runtime_min` (the UI estimates it
  from the charge when left blank).
- CLI: `set-mode percent|runtime`, `set-threshold vm-min|host-min <min>`,
  `simulate <pct> --runtime <min>`; `status` shows the mode.

### Changed

- The worker now logs one sample line every minute on battery (charge,
  runtime, load), not only before the guest stage. These lines feed the
  outage stats.
- Stage log lines and notification emails name the threshold in the active
  mode, e.g. "Guest threshold reached (14 min runtime; battery 72%, runtime
  13m 50s)".
- The warning email is suppressed once the guest stage is reached, in either
  mode (previously compared against the % threshold only).

## [0.4.0] — Daily self-test and worker watchdog

### Added

- **Daily self-test**, run by the cron worker at a configurable hour. Checks
  the UPS (readable, on mains, no replace-battery flag), every enabled host
  (key-based SSH works *and* `qm list` / `pct list` run), the SMTP relay
  (connect + EHLO + NOOP, nothing sent) and the worker heartbeat. Emails on
  failure (daily until fixed), once on recovery, and optionally on success.
  Waits until any outage is over and runs after the shutdown decision, so it
  can't delay a shutdown.
- **Worker watchdog** in the web app: emails if the cron worker hasn't run for
  10 minutes, and again when it recovers. A stopped worker means no outage
  protection at all, and it can't report that itself.
- **Worker heartbeat**: the worker writes `/var/run/power_monitor.heartbeat`
  every tick (not under `DRY_RUN`).
- **Self-test card** in the web UI (results table, "Run self-test now",
  enable / hour / email-on-success settings, server clock shown so the hour
  is unambiguous) and header pills for worker age and last self-test result.
- `./manage.py self-test`, `GET /api/selftest`, `POST /api/selftest/config`,
  `POST /api/selftest/run`.
- `selftest` block in `config.json` (defaults: enabled, 07:00, no success
  email). Results persist in `selftest_last.json` next to `config.json`.

### Fixed

- Footer text no longer says edits prompt for HTTP Basic Auth (the UI has
  used a login form since 0.2.0).

## [0.3.2] — Reliable guest shutdown on hosts with many or slow VMs

### Fixed

- **Guest shutdown could stop partway on a host.** The guest stage ran
  `xargs qm shutdown`, which waits for each guest to fully stop before
  starting the next. With several guests, or one slow Windows VM, it ran past
  the SSH timeout; the session was killed and the remaining guests on that
  host were never asked to shut down. Seen in a real outage as `exit 124`.
  Guest shutdowns are now started detached (`setsid -f`) on the host, so SSH
  returns in about a second and all guests shut down in parallel.

### Added

- **Retry for failed hosts.** If a host's guest-shutdown SSH fails
  (unreachable, timeout), the host is recorded in the state file
  (`guest_retry`) and retried every minute until it succeeds or the host
  stage fires. Other hosts are not re-contacted. New `decide_action` result:
  `retry_guests`.
- **Hosts are contacted in parallel** in both the guest and host stages, so
  one slow or unreachable host no longer delays the rest.

## [0.3.1] — Handle reinstalled hosts (stale SSH host keys)

### Fixed

- **Re-adding a reinstalled host failed with "REMOTE HOST IDENTIFICATION HAS
  CHANGED".** Removing a host only edited `config.json`; the old host keys
  stayed in `/root/.ssh/known_hosts`, so SSH (and Push key) refused the
  rebuilt machine's new keys.

### Added

- **"Reset host key" button** on each host row, `POST
  /api/host/<ip>/reset-hostkey`, and `manage.py forget-key <ip>`. Forgets the
  saved key and shows the host's current fingerprint so you can confirm it.
- **Removing a host now also forgets its saved host key.**
- **Push key forgets the old host key first.** Typing the host's root
  password is the explicit "trust this host" step, so the new key is
  recorded on connect.
- **Clear error text.** A changed-key failure now says to use Reset host key
  instead of showing SSH's raw warning banner.

### Unchanged (deliberately)

- The cron worker's shutdown commands still **refuse** a changed host key.
  Nothing on the outage path auto-trusts a new key, so a spoofed host can't
  be accepted silently during a power event.

### Docs

- Removed site-specific placeholder text from the web UI form hints.

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
