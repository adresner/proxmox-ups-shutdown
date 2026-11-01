"""Shared helpers for the UPS shutdown orchestrator.

Everything that touches the config, the UPS, SSH, or the state file lives here
so the cron worker, the CLI, and the web UI all behave identically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import socket
import ssl
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

EVENT_NAMES = ("warning", "guests_shutdown", "hosts_shutdown", "power_restored")

CONFIG_PATH = Path("/root/config.json")
NUT_UPS_CONF = Path("/etc/nut/ups.conf")


@dataclass
class Host:
    ip: str
    label: str
    enabled: bool
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Host":
        return cls(ip=d["ip"], label=d.get("label", d["ip"]),
                   enabled=bool(d.get("enabled", True)), note=d.get("note", ""))

    def to_dict(self) -> dict:
        return {"ip": self.ip, "label": self.label,
                "enabled": self.enabled, "note": self.note}


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    tmp.replace(CONFIG_PATH)


def enabled_hosts(cfg: dict) -> list[Host]:
    return [Host.from_dict(h) for h in cfg.get("hosts", []) if h.get("enabled")]


def all_hosts(cfg: dict) -> list[Host]:
    return [Host.from_dict(h) for h in cfg.get("hosts", [])]


# --- UPS -------------------------------------------------------------------

def upsc(ups_name: str, var: str | None = None) -> str:
    cmd = ["upsc", ups_name] + ([var] if var else [])
    return subprocess.check_output(cmd, stderr=subprocess.DEVNULL,
                                   timeout=10).decode("utf-8").strip()


def read_ups(ups_name: str) -> dict[str, Any]:
    """Return a dict of all variables from `upsc <ups>`, plus derived fields.

    Raises subprocess.CalledProcessError on read failure.
    """
    blob = upsc(ups_name)
    data: dict[str, Any] = {}
    for line in blob.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip()
    status = data.get("ups.status", "")
    data["_on_battery"] = "OB" in status or "LB" in status
    data["_online"] = "OL" in status
    try:
        data["_charge"] = int(data.get("battery.charge", "0"))
    except ValueError:
        data["_charge"] = 0
    try:
        data["_runtime_s"] = int(data.get("battery.runtime", "0"))
    except ValueError:
        data["_runtime_s"] = 0
    return data


# --- State -----------------------------------------------------------------

NORMAL = "NORMAL"
GUESTS_DOWN = "GUESTS_DOWN"
HOSTS_DOWN = "HOSTS_DOWN"


def _read_state_blob(cfg: dict) -> dict:
    """Return {'state': str, 'sent': list} from disk.

    Accepts the legacy plain-text format ("NORMAL"/"GUESTS_DOWN"/"HOSTS_DOWN")
    and wraps it into the new dict form on first read.
    """
    path = Path(cfg["paths"]["state_file"])
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return {"state": NORMAL, "sent": []}
    if not text:
        return {"state": NORMAL, "sent": []}
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "state" in data:
            data.setdefault("sent", [])
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return {"state": text, "sent": []}


def _write_state_blob(cfg: dict, data: dict) -> None:
    path = Path(cfg["paths"]["state_file"])
    path.write_text(json.dumps(data))


def read_state(cfg: dict) -> str:
    return _read_state_blob(cfg)["state"]


def write_state(cfg: dict, value: str, *,
                guest_retry: list[str] | None = None) -> None:
    """Update state value.

    Returning to NORMAL clears the 'sent' notification list. `guest_retry`
    (IPs whose guest-shutdown SSH failed) is stored only while GUESTS_DOWN;
    any other state clears it.
    """
    data = _read_state_blob(cfg)
    data["state"] = value
    if value == NORMAL:
        data["sent"] = []
    if value == GUESTS_DOWN and guest_retry is not None:
        data["guest_retry"] = list(guest_retry)
    elif value != GUESTS_DOWN:
        data.pop("guest_retry", None)
    _write_state_blob(cfg, data)


def read_guest_retry(cfg: dict) -> list[str]:
    """Hosts whose guest shutdown failed and should be retried next tick."""
    return list(_read_state_blob(cfg).get("guest_retry", []))


def has_notified(cfg: dict, event: str) -> bool:
    return event in _read_state_blob(cfg).get("sent", [])


def mark_notified(cfg: dict, event: str) -> None:
    data = _read_state_blob(cfg)
    sent = data.setdefault("sent", [])
    if event not in sent:
        sent.append(event)
    _write_state_blob(cfg, data)


# --- SSH / shutdown --------------------------------------------------------

# Guest shutdowns are fired *detached* (setsid -f) so the SSH call returns in
# about a second. The old form (`xargs qm shutdown`) waited for each guest to
# stop, one after another; a slow Windows VM pushed it past the SSH timeout,
# the session was killed, and the remaining guests on that host were skipped
# (seen in a real outage as exit 124 on two hosts). Detached, each
# `qm/pct shutdown` keeps running on the host after SSH disconnects, and all
# guests on a host shut down in parallel. Output lists the IDs started.
# qm list columns: VMID NAME STATUS ...   pct list columns: VMID Status Lock Name
VM_SHUTDOWN_CMD = (
    "for id in $(qm list | awk 'NR>1 && $3==\"running\" {print $1}'); do "
    "setsid -f qm shutdown $id </dev/null >/dev/null 2>&1; echo vm:$id; done; "
    "echo started")
CT_SHUTDOWN_CMD = (
    "for id in $(pct list | awk 'NR>1 && $2==\"running\" {print $1}'); do "
    "setsid -f pct shutdown $id </dev/null >/dev/null 2>&1; echo ct:$id; done; "
    "echo started")
HOST_SHUTDOWN_CMD = "shutdown -h now"


KNOWN_HOSTS = Path("/root/.ssh/known_hosts")
_HOSTKEY_CHANGED_MARKERS = ("REMOTE HOST IDENTIFICATION HAS CHANGED",
                            "Host key verification failed")
HOSTKEY_CHANGED_HINT = ("host key changed (host was reinstalled?). Click "
                        "'Reset host key' or run `manage.py forget-key <ip>`, "
                        "then Test SSH again.")


def is_hostkey_changed(err: str) -> bool:
    return any(m in (err or "") for m in _HOSTKEY_CHANGED_MARKERS)


def forget_host_key(ip: str) -> tuple[bool, str]:
    """Remove every saved host key for `ip` from root's known_hosts.

    Needed when a host is reinstalled (new host keys). Safe to call when no
    entry exists. Never called automatically from the shutdown path.
    """
    if not KNOWN_HOSTS.exists():
        return True, "no known_hosts file"
    had = subprocess.run(["ssh-keygen", "-F", ip, "-f", str(KNOWN_HOSTS)],
                         capture_output=True, text=True).returncode == 0
    if not had:
        return True, "no saved key"
    out = subprocess.run(["ssh-keygen", "-R", ip, "-f", str(KNOWN_HOSTS)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip()
    return True, "old host key removed"


def scan_host_fingerprint(ip: str, timeout: int = 5) -> str:
    """Return the host's current ed25519 fingerprint (for display), or ''."""
    try:
        scan = subprocess.run(["ssh-keyscan", "-T", str(timeout), "-t", "ed25519", ip],
                              capture_output=True, text=True, timeout=timeout + 5)
        if not scan.stdout.strip():
            return ""
        fp = subprocess.run(["ssh-keygen", "-lf", "-"], input=scan.stdout,
                            capture_output=True, text=True)
        return fp.stdout.split()[1] if fp.returncode == 0 and fp.stdout else ""
    except Exception:
        return ""


def ssh_run(host: Host, cfg: dict, command: str,
            *, dry_run: bool = False, log=None) -> tuple[bool, str]:
    """Run a command on a host via SSH. Returns (ok, output_or_error)."""
    if dry_run:
        msg = f"[DRY-RUN] {host.label} ({host.ip}): {command}"
        if log:
            log.info(msg)
        return True, msg

    ssh_cfg = cfg.get("ssh", {})
    user = ssh_cfg.get("user", "root")
    timeout = int(ssh_cfg.get("connect_timeout", 5))
    args = [
        "ssh",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{user}@{host.ip}",
        command,
    ]
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=timeout + 10)
        if out.returncode == 0:
            if log:
                log.info(f"OK   {host.label} ({host.ip}): {command}")
            return True, (out.stdout or "ok").strip()
        err = (out.stderr or out.stdout or f"exit {out.returncode}").strip()
        if is_hostkey_changed(err):
            err = HOSTKEY_CHANGED_HINT
        if log:
            log.error(f"FAIL {host.label} ({host.ip}): {err}")
        return False, err
    except subprocess.TimeoutExpired:
        if log:
            log.error(f"TIMEOUT {host.label} ({host.ip})")
        return False, "ssh timeout"
    except Exception as e:
        if log:
            log.error(f"ERR  {host.label} ({host.ip}): {e}")
        return False, str(e)


def _parallel(hosts: list[Host], fn) -> list[dict]:
    """Run fn(host) for every host concurrently; results keep host order."""
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
        return list(pool.map(fn, hosts))


def shutdown_guests(cfg: dict, *, dry_run: bool = False, log=None,
                    only_ips: list[str] | None = None) -> list[dict]:
    """Ask every enabled host (or just `only_ips`) to shut down its guests."""
    hosts = [h for h in enabled_hosts(cfg)
             if only_ips is None or h.ip in only_ips]

    def one(host: Host) -> dict:
        ok_vm, msg_vm = ssh_run(host, cfg, VM_SHUTDOWN_CMD, dry_run=dry_run, log=log)
        ok_ct, msg_ct = ssh_run(host, cfg, CT_SHUTDOWN_CMD, dry_run=dry_run, log=log)
        return {"host": host.ip, "label": host.label,
                "ok": ok_vm and ok_ct,
                "vm": {"ok": ok_vm, "msg": msg_vm},
                "ct": {"ok": ok_ct, "msg": msg_ct}}

    return _parallel(hosts, one)


def shutdown_hosts(cfg: dict, *, dry_run: bool = False, log=None) -> list[dict]:
    def one(host: Host) -> dict:
        ok, msg = ssh_run(host, cfg, HOST_SHUTDOWN_CMD, dry_run=dry_run, log=log)
        return {"host": host.ip, "label": host.label, "ok": ok, "msg": msg}

    return _parallel(enabled_hosts(cfg), one)


def test_ssh(host: Host, cfg: dict) -> tuple[bool, str]:
    """Cheap connectivity check — runs `hostname` over SSH."""
    return ssh_run(host, cfg, "hostname")


def push_ssh_key(host: Host, cfg: dict, password: str) -> tuple[bool, str]:
    """Run ssh-copy-id non-interactively using sshpass.

    The password is passed via the SSHPASS env var (not argv), so it doesn't
    appear in process listings. It is never persisted to disk or logged.
    """
    ssh_cfg = cfg.get("ssh", {})
    user = ssh_cfg.get("user", "root")
    key_path = ssh_cfg.get("key_path", "/root/.ssh/id_rsa")
    pubkey_path = key_path + ".pub"
    if not Path(pubkey_path).exists():
        return False, f"missing public key at {pubkey_path}"
    if not password:
        return False, "empty password"
    # Pushing a key with the root password is the explicit "trust this host"
    # moment, so drop any stale host key first (e.g. host was reinstalled).
    # accept-new below then records the host's current key.
    forget_host_key(host.ip)
    env = os.environ.copy()
    env["SSHPASS"] = password
    args = [
        "sshpass", "-e",
        "ssh-copy-id",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-i", pubkey_path,
        f"{user}@{host.ip}",
    ]
    try:
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=30, env=env)
    except FileNotFoundError:
        return False, "sshpass not installed (apt install sshpass)"
    except subprocess.TimeoutExpired:
        return False, "ssh-copy-id timeout"
    except Exception as e:
        return False, str(e)
    msg = (out.stdout + out.stderr).strip()
    if out.returncode == 0:
        return True, msg or "key installed"
    # sshpass exit codes: 5 = wrong password, 6 = host key verification failed
    if out.returncode == 5:
        return False, "authentication failed (wrong password)"
    if out.returncode == 6 or is_hostkey_changed(msg):
        return False, HOSTKEY_CHANGED_HINT
    return False, msg or f"ssh-copy-id exit {out.returncode}"


# --- Decision logic --------------------------------------------------------

def decide_action(cfg: dict, status: str, charge: int, current_state: str,
                  guest_retry_pending: bool = False) -> str:
    """Pure function: given UPS status + charge + current state, return one of
    'noop', 'shutdown_guests', 'retry_guests', 'shutdown_hosts', 'reset_normal'.

    'retry_guests' re-runs the guest stage only on hosts whose previous
    attempt failed (unreachable, timeout), every tick while still on battery
    and above the host threshold.
    """
    th = cfg["thresholds"]
    vm_pct = th["vm_shutdown_pct"]
    host_pct = th["host_shutdown_pct"]
    on_battery = "OB" in status or "LB" in status
    online = "OL" in status

    if online and current_state != NORMAL:
        return "reset_normal"

    if on_battery:
        if charge <= host_pct and current_state != HOSTS_DOWN:
            return "shutdown_hosts"
        if host_pct < charge <= vm_pct and current_state == NORMAL:
            return "shutdown_guests"
        if (charge > host_pct and current_state == GUESTS_DOWN
                and guest_retry_pending):
            return "retry_guests"

    return "noop"


# --- Logging ---------------------------------------------------------------

def get_logger(cfg: dict, name: str = "power_monitor") -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    handler = logging.FileHandler(cfg["paths"]["log_file"])
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log


# --- UPS config (NUT) ------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_COMM_RE = re.compile(r"^\S{1,64}$")
_UPS_EDITABLE_KEYS = ("port", "community", "pollfreq")


def validate_ups_input(name: str, host: str, community: str,
                       pollfreq) -> str | None:
    if not _NAME_RE.match(name or ""):
        return "name: must be 1-32 chars, letters/digits/_/- only"
    if not _HOST_RE.match(host or ""):
        return "host: must be an IP address or hostname"
    if not _COMM_RE.match(community or ""):
        return "community: must be 1-64 non-whitespace chars"
    try:
        pf = int(pollfreq)
    except (TypeError, ValueError):
        return "pollfreq: must be integer"
    if not (1 <= pf <= 300):
        return "pollfreq: must be between 1 and 300 seconds"
    return None


def read_ups_section(path: Path, name: str) -> dict[str, str]:
    """Return the key=value pairs in the [name] section of ups.conf."""
    values: dict[str, str] = {}
    in_section = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                break
            in_section = stripped == f"[{name}]"
            continue
        if in_section and "=" in stripped and not stripped.startswith("#"):
            k, _, v = stripped.partition("=")
            values[k.strip()] = v.strip()
    return values


def write_ups_section(path: Path, old_name: str, new_name: str,
                      updates: dict[str, str]) -> None:
    """Rewrite ups.conf, modifying the [old_name] section in place.

    The section header is renamed to [new_name] if different. Keys listed in
    `updates` are replaced; all other keys, comments, and out-of-section lines
    are preserved verbatim.
    """
    lines = path.read_text().splitlines(keepends=True)
    out: list[str] = []
    in_section = False
    found_section = False
    written_keys: set[str] = set()

    def emit_remaining(indent: str) -> None:
        for k, v in updates.items():
            if k not in written_keys:
                out.append(f"{indent}{k} = {v}\n")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                emit_remaining("    ")
                in_section = False
            if stripped == f"[{old_name}]":
                found_section = True
                in_section = True
                out.append(f"[{new_name}]\n")
                continue
        if in_section and "=" in stripped and not stripped.startswith("#"):
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}{k} = {updates[k]}\n")
                written_keys.add(k)
                continue
        out.append(line)

    if in_section:
        emit_remaining("    ")

    if not found_section:
        raise ValueError(f"section [{old_name}] not found in {path}")

    tmp = path.with_suffix(".conf.tmp")
    tmp.write_text("".join(out))
    try:
        os.chmod(tmp, 0o640)
        # match group ownership of original file so nut user can still read it
        stat = path.stat()
        os.chown(tmp, stat.st_uid, stat.st_gid)
    except OSError:
        pass
    tmp.replace(path)


def reload_nut() -> tuple[bool, str]:
    """Stop NUT, re-enumerate driver instances from ups.conf, start NUT.

    Synchronous stop+start. Driver stop time is bounded to ~10s by the
    `/etc/systemd/system/nut-driver@.service.d/override.conf` drop-in, so a
    hung snmp-ups driver (e.g. unreachable UPS) can't tie us up.

    The enumerator turns each [section] in ups.conf into a `nut-driver@<name>`
    systemd instance, so it must run before restart whenever the section name
    might have changed.
    """
    try:
        subprocess.run(["systemctl", "stop", "nut.target"],
                       capture_output=True, timeout=20, check=False)
        subprocess.run(["systemctl", "reset-failed", "nut.target",
                        "nut-server.service", "nut-driver.target"],
                       capture_output=True, timeout=10, check=False)
        out = subprocess.run(
            ["systemctl", "restart", "nut-driver-enumerator.service"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return False, (out.stderr or out.stdout or "enumerator failed").strip()
        out = subprocess.run(
            ["systemctl", "start", "nut.target"],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return False, (out.stderr or out.stdout or "start failed").strip()
        return True, "ok"
    except subprocess.TimeoutExpired as e:
        return False, f"systemctl timeout: {e}"


def verify_ups(name: str, timeout_s: int = 25) -> tuple[bool, str]:
    """Poll `upsc <name> battery.charge` until it succeeds or timeout."""
    deadline = time.monotonic() + timeout_s
    last_err = "no response"
    while time.monotonic() < deadline:
        try:
            v = upsc(name, "battery.charge")
            if v.strip().isdigit():
                return True, f"battery.charge={v.strip()}"
            last_err = f"unexpected value: {v!r}"
        except subprocess.CalledProcessError as e:
            last_err = (e.stderr or b"").decode("utf-8", "replace").strip() \
                       or f"exit {e.returncode}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1)
    return False, last_err


def get_ups_config(cfg: dict) -> dict[str, str]:
    """Return current UPS connection settings (name + editable section keys)."""
    name = cfg["ups_name"]
    try:
        section = read_ups_section(NUT_UPS_CONF, name)
    except FileNotFoundError:
        section = {}
    return {
        "name": name,
        "host": section.get("port", ""),
        "community": section.get("community", ""),
        "pollfreq": section.get("pollfreq", ""),
        "snmp_version": section.get("snmp_version", ""),
        "driver": section.get("driver", ""),
    }


def update_ups_config(cfg: dict, new: dict) -> tuple[bool, str]:
    """Apply new UPS settings with backup + validate + verify + rollback.

    `new` keys: name, host, community, pollfreq. Caller must have already
    authenticated. Returns (ok, message).
    """
    err = validate_ups_input(new.get("name"), new.get("host"),
                             new.get("community"), new.get("pollfreq"))
    if err:
        return False, err

    old_name = cfg["ups_name"]
    new_name = new["name"]
    ups_conf_backup = NUT_UPS_CONF.read_text()
    cfg_backup = json.dumps(cfg)

    def rollback(reason: str) -> tuple[bool, str]:
        NUT_UPS_CONF.write_text(ups_conf_backup)
        save_config(json.loads(cfg_backup))
        reload_nut()
        # Wait for the restored config to come back online so the response
        # reflects real system state, not just "we queued a restart".
        ok, vmsg = verify_ups(old_name, timeout_s=15)
        if ok:
            return False, f"rolled back ok — {reason}"
        return False, f"rolled back but NUT still recovering ({vmsg}) — {reason}"

    try:
        updates = {
            "port": new["host"],
            "community": new["community"],
            "pollfreq": str(int(new["pollfreq"])),
        }
        write_ups_section(NUT_UPS_CONF, old_name, new_name, updates)

        cfg["ups_name"] = new_name
        save_config(cfg)

        ok, msg = reload_nut()
        if not ok:
            return rollback(f"reload failed: {msg}")

        ok, msg = verify_ups(new_name, timeout_s=25)
        if not ok:
            return rollback(f"UPS unreachable after reload: {msg}")

        return True, "applied"
    except Exception as e:
        return rollback(str(e))


# --- Notifications ---------------------------------------------------------

def _parse_recipients(raw) -> list[str]:
    if isinstance(raw, list):
        return [a.strip() for a in raw if a and a.strip()]
    if isinstance(raw, str):
        return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
    return []


def send_email(cfg: dict, subject: str, body: str) -> tuple[bool, str]:
    """Send a single email via the SMTP server configured under notifications.*.

    Returns (ok, message). All values come from config; nothing is hardcoded.
    """
    notif = cfg.get("notifications", {})
    host = (notif.get("smtp_host") or "").strip()
    if not host:
        return False, "smtp_host not configured"
    sender = (notif.get("from_address") or "").strip()
    if not sender:
        return False, "from_address not configured"
    recipients = _parse_recipients(notif.get("to_addresses"))
    if not recipients:
        return False, "to_addresses not configured"

    port = int(notif.get("smtp_port") or 25)
    use_tls = bool(notif.get("smtp_use_tls"))
    user = (notif.get("smtp_user") or "").strip()
    password = notif.get("smtp_password") or ""

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    # Self-signed certs are common on internal SMTP relays; encrypt but skip
    # hostname/CA verification. Confidentiality without PKI is the right
    # tradeoff for a homelab tool talking to its own relay.
    tls_ctx = ssl.create_default_context()
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE

    try:
        if use_tls and port == 465:
            client = smtplib.SMTP_SSL(host, port, timeout=15, context=tls_ctx)
        else:
            client = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                client.starttls(context=tls_ctx)
        try:
            if user:
                client.login(user, password)
            client.send_message(msg)
        finally:
            try: client.quit()
            except Exception: pass
        return True, f"sent to {', '.join(recipients)}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"auth failed: {e}"
    except (smtplib.SMTPException, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def _format_event_email(cfg: dict, event: str, ups: dict) -> tuple[str, str]:
    """Return (subject, body) for a given event using current UPS data."""
    charge = ups.get("_charge", "?")
    runtime_s = ups.get("_runtime_s", 0)
    runtime_str = f"{runtime_s // 60}m {runtime_s % 60}s" if runtime_s else "unknown"
    model = ups.get("device.model", cfg.get("ups_name", "UPS"))
    hostname = socket.gethostname()
    enabled = enabled_hosts(cfg)
    host_list = ", ".join(f"{h.label} ({h.ip})" for h in enabled) or "(none enabled)"
    th = cfg["thresholds"]

    if event == "warning":
        subject = f"[nutshutdown] UPS battery warning — {charge}%"
        body = (f"The UPS reports it is on battery and the charge has dropped "
                f"to {charge}%.\n\nRuntime estimate: {runtime_str}\n"
                f"Next stage: guest shutdown at {th['vm_shutdown_pct']}% "
                f"(then host shutdown at {th['host_shutdown_pct']}%).\n\n"
                f"UPS: {model}\nEnabled hosts: {host_list}\n"
                f"Orchestrator: {hostname}\n")
    elif event == "guests_shutdown":
        subject = f"[nutshutdown] Guest shutdown started — battery {charge}%"
        body = (f"Battery dropped to {charge}% (<= {th['vm_shutdown_pct']}%). "
                f"qm shutdown and pct shutdown have been issued to every "
                f"enabled host.\n\nRuntime estimate: {runtime_str}\n"
                f"Hosts: {host_list}\nOrchestrator: {hostname}\n")
    elif event == "hosts_shutdown":
        subject = f"[nutshutdown] Host shutdown started — battery {charge}%"
        body = (f"Battery dropped to {charge}% (<= {th['host_shutdown_pct']}%). "
                f"shutdown -h now has been issued to every enabled host. This "
                f"is the last email this outage.\n\nHosts: {host_list}\n"
                f"Orchestrator: {hostname}\n")
    elif event == "power_restored":
        subject = "[nutshutdown] Power restored"
        body = (f"The UPS reports it is back online (charge {charge}%). The "
                f"state machine has been reset to NORMAL; the next outage "
                f"will fire a fresh round of notifications.\n\n"
                f"Hosts: {host_list}\nOrchestrator: {hostname}\n")
    else:
        subject = f"[nutshutdown] {event}"
        body = f"Event: {event}\nCharge: {charge}%\nHosts: {host_list}\n"
    return subject, body


def notify_event(cfg: dict, event: str, ups: dict,
                 *, dry_run: bool = False, log=None) -> tuple[bool, str]:
    """Send the email for an event if it's enabled and hasn't fired this outage.

    Marks as sent on success (so the per-minute cron doesn't re-fire).
    Skips silently if the event is disabled or already sent.
    """
    notif = cfg.get("notifications", {})
    events = notif.get("events", {})
    if not events.get(event, False):
        return False, "event disabled"
    if has_notified(cfg, event):
        return False, "already sent this outage"
    if dry_run:
        if log:
            log.info(f"[DRY-RUN] would notify event={event}")
        return True, "[DRY-RUN]"
    subject, body = _format_event_email(cfg, event, ups)
    ok, msg = send_email(cfg, subject, body)
    if ok:
        mark_notified(cfg, event)
        if log:
            log.info(f"email sent: event={event} ({msg})")
    else:
        if log:
            log.error(f"email FAILED: event={event} — {msg}")
    return ok, msg


def validate_notifications_input(data: dict) -> str | None:
    """Lightweight sanity check on the notifications form input. Returns error or None."""
    if data.get("smtp_host") and not isinstance(data["smtp_host"], str):
        return "smtp_host must be a string"
    try:
        port = int(data.get("smtp_port") or 25)
    except (TypeError, ValueError):
        return "smtp_port must be integer"
    if not (1 <= port <= 65535):
        return "smtp_port must be 1-65535"
    try:
        wp = int(data.get("warning_pct") or 80)
    except (TypeError, ValueError):
        return "warning_pct must be integer"
    if not (1 <= wp <= 100):
        return "warning_pct must be 1-100"
    recipients = _parse_recipients(data.get("to_addresses"))
    for r in recipients:
        if "@" not in r:
            return f"invalid recipient address: {r!r}"
    return None


def tail_log(cfg: dict, lines: int = 50) -> list[str]:
    path = Path(cfg["paths"]["log_file"])
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = 4096
        data = b""
        while size > 0 and data.count(b"\n") <= lines:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


# --- Self-test + worker heartbeat ------------------------------------------
#
# A daily check that everything an outage depends on still works: the UPS is
# readable, every enabled host accepts SSH *and* can run qm/pct, the SMTP
# relay answers, and the cron worker is alive. Failures are emailed (once per
# scheduled run, i.e. daily until fixed) and a recovery email is sent when a
# failing self-test passes again. Nothing here shuts anything down.

SELFTEST_DEFAULTS = {"enabled": True, "hour": 7, "email_on_success": False}
WORKER_STALE_S = 600   # worker heartbeat older than this = worker not running
SELFTEST_HOST_CMD = "hostname && qm list >/dev/null && pct list >/dev/null && echo pve-ok"


def selftest_cfg(cfg: dict) -> dict:
    return {**SELFTEST_DEFAULTS, **(cfg.get("selftest") or {})}


def _heartbeat_path(cfg: dict) -> Path:
    return Path(cfg["paths"].get("heartbeat_file", "/var/run/power_monitor.heartbeat"))


def _selftest_path(cfg: dict) -> Path:
    return Path(cfg["paths"].get("selftest_file",
                                 str(CONFIG_PATH.parent / "selftest_last.json")))


def write_heartbeat(cfg: dict) -> None:
    """Called by the cron worker on every tick (not in DRY_RUN)."""
    _heartbeat_path(cfg).write_text(str(int(time.time())))


def heartbeat_age(cfg: dict) -> int | None:
    """Seconds since the worker last ran, or None if it never has (since boot)."""
    try:
        return int(time.time()) - int(_heartbeat_path(cfg).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def load_selftest(cfg: dict) -> dict | None:
    try:
        return json.loads(_selftest_path(cfg).read_text())
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def save_selftest(cfg: dict, result: dict) -> None:
    path = _selftest_path(cfg)
    if "last_scheduled_date" not in result:
        # A manual run must not reset "already ran today's scheduled test".
        prev = load_selftest(cfg) or {}
        if prev.get("last_scheduled_date"):
            result["last_scheduled_date"] = prev["last_scheduled_date"]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2))
    os.replace(tmp, path)


def check_smtp(cfg: dict) -> tuple[bool, str]:
    """Connect to the configured SMTP server and say hello. Sends nothing."""
    notif = cfg.get("notifications", {}) or {}
    host = (notif.get("smtp_host") or "").strip()
    if not host:
        return True, "email not configured (skipped); self-test results only show in the web UI"
    port = int(notif.get("smtp_port") or 25)
    use_tls = bool(notif.get("smtp_use_tls"))
    user = (notif.get("smtp_user") or "").strip()
    tls_ctx = ssl.create_default_context()
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_NONE
    try:
        if use_tls and port == 465:
            client = smtplib.SMTP_SSL(host, port, timeout=10, context=tls_ctx)
        else:
            client = smtplib.SMTP(host, port, timeout=10)
            if use_tls:
                client.starttls(context=tls_ctx)
        try:
            code, _ = client.ehlo()
            if user:
                client.login(user, notif.get("smtp_password") or "")
            code, _ = client.noop()
        finally:
            try: client.quit()
            except Exception: pass
        if code != 250:
            return False, f"{host}:{port} answered {code} to NOOP"
        return True, f"{host}:{port} reachable" + (" (login ok)" if user else "")
    except smtplib.SMTPAuthenticationError as e:
        return False, f"{host}:{port} login failed: {e}"
    except (smtplib.SMTPException, OSError) as e:
        return False, f"{host}:{port} unreachable: {type(e).__name__}: {e}"


def run_selftest(cfg: dict, *, trigger: str = "manual") -> dict:
    """Run every check and return a result dict. Read-only on all hosts."""
    checks: list[dict] = []

    def add(name: str, ok: bool, msg: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "msg": msg})

    # 1. UPS
    try:
        ups = read_ups(cfg["ups_name"])
        status = ups.get("ups.status", "")
        charge, runtime = ups["_charge"], ups["_runtime_s"]
        detail = f"status {status}, charge {charge}%, runtime {runtime // 60}m"
        if "RB" in status.split():
            add("UPS", False, f"UPS reports REPLACE BATTERY ({detail})")
        elif "OL" not in status.split():
            add("UPS", False, f"not on mains power ({detail})")
        else:
            add("UPS", True, detail)
    except Exception as e:
        add("UPS", False, f"cannot read UPS via upsc: {e}")

    # 2. Hosts: SSH + qm/pct usable (exactly what the shutdown path needs)
    hosts = enabled_hosts(cfg)
    if not hosts:
        add("Hosts", False, "no hosts are enabled; nothing would be shut down")

    def one(h: Host) -> dict:
        ok, msg = ssh_run(h, cfg, SELFTEST_HOST_CMD)
        if ok and "pve-ok" in msg:
            name = msg.splitlines()[0] if msg else h.label
            return {"name": f"Host {h.label}", "ok": True,
                    "msg": f"{h.ip}: SSH ok (hostname {name}), qm/pct ok"}
        if ok:
            return {"name": f"Host {h.label}", "ok": False,
                    "msg": f"{h.ip}: SSH ok but qm/pct failed: {msg}"}
        return {"name": f"Host {h.label}", "ok": False, "msg": f"{h.ip}: {msg}"}

    checks.extend(_parallel(hosts, one))

    # 3. Email relay
    ok, msg = check_smtp(cfg)
    add("Email relay", ok, msg)

    # 4. Cron worker heartbeat
    age = heartbeat_age(cfg)
    if age is None:
        add("Cron worker", False, "no heartbeat yet; is the cron entry for power_monitor.py installed?")
    elif age > WORKER_STALE_S:
        add("Cron worker", False, f"last ran {age // 60} min ago; cron or the worker is broken")
    else:
        add("Cron worker", True, f"last ran {age}s ago")

    now = time.time()
    return {
        "ts": int(now),
        "time": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(now)),
        "date": time.strftime("%Y-%m-%d", time.localtime(now)),
        "trigger": trigger,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def format_selftest(result: dict) -> str:
    lines = [f"Self-test {'PASSED' if result['ok'] else 'FAILED'} at {result['time']} "
             f"({result['trigger']})", ""]
    for c in result["checks"]:
        lines.append(f"  [{'OK ' if c['ok'] else 'FAIL'}] {c['name']}: {c['msg']}")
    return "\n".join(lines)


def selftest_notify(cfg: dict, result: dict, prev: dict | None, *, log=None) -> None:
    """Email on failure (every scheduled run), on recovery, and optionally on success."""
    st = selftest_cfg(cfg)
    hostname = socket.gethostname()
    failed = [c for c in result["checks"] if not c["ok"]]
    if failed:
        subject = (f"[nutshutdown] SELF-TEST FAILED: {len(failed)} problem(s), "
                   f"{', '.join(c['name'] for c in failed)}")
        intro = ("The daily self-test found problems that could stop a clean "
                 "shutdown during a power outage. Fix these, then use \"Run "
                 "self-test now\" in the web UI to confirm.\n\n")
    elif prev is not None and not prev.get("ok", True):
        subject = "[nutshutdown] Self-test passing again"
        intro = "The problems reported by the previous self-test are resolved.\n\n"
    elif st["email_on_success"]:
        subject = "[nutshutdown] Self-test OK"
        intro = ""
    else:
        return
    body = f"{intro}{format_selftest(result)}\n\nOrchestrator: {hostname}\n"
    ok, msg = send_email(cfg, subject, body)
    if log:
        (log.info if ok else log.error)(
            f"self-test email {'sent' if ok else 'FAILED'}: {subject} ({msg})")


def maybe_run_scheduled_selftest(cfg: dict, *, log=None) -> dict | None:
    """Run the daily self-test if it's due. Called by the cron worker each tick.

    Due = enabled, local hour >= configured hour, and no scheduled run yet
    today. Skipped (and retried next tick) while an outage is in progress, so
    it never competes with real shutdown work or sends confusing emails.
    """
    st = selftest_cfg(cfg)
    if not st["enabled"]:
        return None
    now = time.localtime()
    if now.tm_hour < int(st["hour"]):
        return None
    prev = load_selftest(cfg)
    today = time.strftime("%Y-%m-%d", now)
    if prev and prev.get("last_scheduled_date") == today:
        return None
    if read_state(cfg) != NORMAL:
        return None
    try:
        if read_ups(cfg["ups_name"]).get("_on_battery"):
            return None
    except Exception:
        pass  # unreadable UPS is itself a failure the self-test will report

    result = run_selftest(cfg, trigger="scheduled")
    result["last_scheduled_date"] = today
    save_selftest(cfg, result)
    if log:
        (log.info if result["ok"] else log.error)(
            "SELF-TEST " + ("passed" if result["ok"] else "FAILED: " + "; ".join(
                f"{c['name']}: {c['msg']}" for c in result["checks"] if not c["ok"])))
    selftest_notify(cfg, result, prev, log=log)
    return result
