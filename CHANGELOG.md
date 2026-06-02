# Changelog

All notable changes to this project are documented here.
The format loosely follows [Keep a Changelog](https://keepachangelog.com/),
and the project uses [Semantic Versioning](https://semver.org/).

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
