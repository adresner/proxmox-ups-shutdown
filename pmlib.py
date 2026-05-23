"""Shared helpers for the UPS shutdown orchestrator.

Everything that touches the config, the UPS, SSH, or the state file lives here
so the cron worker, the CLI, and the web UI all behave identically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def read_state(cfg: dict) -> str:
    path = Path(cfg["paths"]["state_file"])
    try:
        return path.read_text().strip() or NORMAL
    except FileNotFoundError:
        return NORMAL


def write_state(cfg: dict, value: str) -> None:
    path = Path(cfg["paths"]["state_file"])
    path.write_text(value)


# --- SSH / shutdown --------------------------------------------------------

VM_SHUTDOWN_CMD = "qm list | grep running | awk '{print $1}' | xargs -r -I % qm shutdown %"
CT_SHUTDOWN_CMD = "pct list | grep running | awk '{print $1}' | xargs -r -I % pct shutdown %"
HOST_SHUTDOWN_CMD = "shutdown -h now"


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


def shutdown_guests(cfg: dict, *, dry_run: bool = False, log=None) -> list[dict]:
    results = []
    for host in enabled_hosts(cfg):
        ok_vm, msg_vm = ssh_run(host, cfg, VM_SHUTDOWN_CMD, dry_run=dry_run, log=log)
        ok_ct, msg_ct = ssh_run(host, cfg, CT_SHUTDOWN_CMD, dry_run=dry_run, log=log)
        results.append({"host": host.ip, "label": host.label,
                        "vm": {"ok": ok_vm, "msg": msg_vm},
                        "ct": {"ok": ok_ct, "msg": msg_ct}})
    return results


def shutdown_hosts(cfg: dict, *, dry_run: bool = False, log=None) -> list[dict]:
    results = []
    for host in enabled_hosts(cfg):
        ok, msg = ssh_run(host, cfg, HOST_SHUTDOWN_CMD, dry_run=dry_run, log=log)
        results.append({"host": host.ip, "label": host.label,
                        "ok": ok, "msg": msg})
    return results


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
    return False, msg or f"ssh-copy-id exit {out.returncode}"


# --- Decision logic --------------------------------------------------------

def decide_action(cfg: dict, status: str, charge: int, current_state: str) -> str:
    """Pure function: given UPS status + charge + current state, return one of
    'noop', 'shutdown_guests', 'shutdown_hosts', 'reset_normal'.
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
