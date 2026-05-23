#!/usr/bin/env python3
"""CLI for managing the UPS shutdown orchestrator.

Examples:
    ./manage.py status
    ./manage.py list
    ./manage.py add 10.0.0.21 --label pve-01
    ./manage.py remove 10.0.0.21
    ./manage.py enable 10.0.0.21
    ./manage.py disable 10.0.0.21
    ./manage.py set-threshold vm 55
    ./manage.py set-threshold host 15
    ./manage.py simulate 45        # dry-run the full logic at a fake charge
    ./manage.py test-ssh           # try `hostname` on every enabled host
    ./manage.py test-ssh 10.0.0.21
    ./manage.py reset-state
    ./manage.py push-key 10.0.0.21   # ssh-copy-id helper
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pmlib


def cmd_status(args) -> int:
    cfg = pmlib.load_config()
    try:
        ups = pmlib.read_ups(cfg["ups_name"])
    except Exception as e:
        print(f"UPS read failed: {e}", file=sys.stderr)
        return 1
    state = pmlib.read_state(cfg)
    print(f"UPS:        {cfg['ups_name']}")
    print(f"  status:   {ups.get('ups.status', '?')}")
    print(f"  charge:   {ups['_charge']}%")
    print(f"  runtime:  {ups['_runtime_s']}s")
    print(f"  model:    {ups.get('device.model', '?')}")
    print(f"State:      {state}")
    print(f"Thresholds: VM<={cfg['thresholds']['vm_shutdown_pct']}%  "
          f"HOST<={cfg['thresholds']['host_shutdown_pct']}%")
    print(f"Hosts (enabled):")
    for h in pmlib.enabled_hosts(cfg):
        print(f"  - {h.ip:<15s} {h.label}")
    print(f"Hosts (disabled):")
    for h in pmlib.all_hosts(cfg):
        if not h.enabled:
            print(f"  - {h.ip:<15s} {h.label}  ({h.note})")
    return 0


def cmd_list(args) -> int:
    cfg = pmlib.load_config()
    for h in pmlib.all_hosts(cfg):
        flag = "ON " if h.enabled else "off"
        print(f"  [{flag}] {h.ip:<15s} {h.label:<20s} {h.note}")
    return 0


def cmd_add(args) -> int:
    cfg = pmlib.load_config()
    if any(h["ip"] == args.ip for h in cfg["hosts"]):
        print(f"Host {args.ip} already exists. Use enable/disable.", file=sys.stderr)
        return 1
    cfg["hosts"].append({
        "ip": args.ip,
        "label": args.label or args.ip,
        "enabled": not args.disabled,
        "note": args.note or "",
    })
    pmlib.save_config(cfg)
    print(f"Added {args.ip} (label={args.label or args.ip}, "
          f"enabled={not args.disabled})")
    if args.push_key:
        return _push_key(args.ip)
    return 0


def cmd_remove(args) -> int:
    cfg = pmlib.load_config()
    before = len(cfg["hosts"])
    cfg["hosts"] = [h for h in cfg["hosts"] if h["ip"] != args.ip]
    if len(cfg["hosts"]) == before:
        print(f"Host {args.ip} not found", file=sys.stderr)
        return 1
    pmlib.save_config(cfg)
    print(f"Removed {args.ip}")
    return 0


def _set_enabled(ip: str, value: bool) -> int:
    cfg = pmlib.load_config()
    for h in cfg["hosts"]:
        if h["ip"] == ip:
            h["enabled"] = value
            pmlib.save_config(cfg)
            print(f"{ip} -> enabled={value}")
            return 0
    print(f"Host {ip} not found", file=sys.stderr)
    return 1


def cmd_enable(args) -> int:
    return _set_enabled(args.ip, True)


def cmd_disable(args) -> int:
    return _set_enabled(args.ip, False)


def cmd_set_threshold(args) -> int:
    cfg = pmlib.load_config()
    key = {"vm": "vm_shutdown_pct", "host": "host_shutdown_pct"}[args.which]
    cfg["thresholds"][key] = int(args.value)
    if cfg["thresholds"]["host_shutdown_pct"] >= cfg["thresholds"]["vm_shutdown_pct"]:
        print("ERROR: host threshold must be lower than VM threshold", file=sys.stderr)
        return 1
    pmlib.save_config(cfg)
    print(f"{key} = {args.value}")
    return 0


def cmd_simulate(args) -> int:
    """Run the full decision logic with a fake charge — no real shutdowns."""
    cfg = pmlib.load_config()
    status = "OB" if args.charge < 100 or args.on_battery else "OL"
    state = pmlib.read_state(cfg)
    action = pmlib.decide_action(cfg, status, args.charge, state)
    print(f"Simulated: status={status} charge={args.charge}% state={state}")
    print(f"Decision:  {action}")
    if action in ("shutdown_guests", "shutdown_hosts"):
        results = (pmlib.shutdown_guests if action == "shutdown_guests"
                   else pmlib.shutdown_hosts)(cfg, dry_run=True)
        for r in results:
            print(f"  -> {r}")
    return 0


def cmd_test_ssh(args) -> int:
    cfg = pmlib.load_config()
    targets = (pmlib.all_hosts(cfg) if not args.ip
               else [pmlib.Host.from_dict(h) for h in cfg["hosts"] if h["ip"] == args.ip])
    if not targets:
        print(f"No host matching {args.ip}", file=sys.stderr)
        return 1
    rc = 0
    for h in targets:
        ok, msg = pmlib.test_ssh(h, cfg)
        flag = "OK " if ok else "FAIL"
        print(f"  [{flag}] {h.ip:<15s} {h.label:<20s} {msg}")
        if not ok:
            rc = 1
    return rc


def cmd_reset_state(args) -> int:
    cfg = pmlib.load_config()
    pmlib.write_state(cfg, pmlib.NORMAL)
    print(f"State reset to {pmlib.NORMAL}")
    return 0


def _push_key(ip: str) -> int:
    print(f"Pushing SSH key to root@{ip} ...")
    rc = subprocess.call(["ssh-copy-id", "-i", "/root/.ssh/id_rsa.pub",
                          f"root@{ip}"])
    return rc


def cmd_push_key(args) -> int:
    return _push_key(args.ip)


def cmd_dump(args) -> int:
    print(json.dumps(pmlib.load_config(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="manage.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show UPS + state + node summary").set_defaults(fn=cmd_status)
    sub.add_parser("list",   help="List all configured hosts").set_defaults(fn=cmd_list)
    sub.add_parser("dump",   help="Print full config JSON").set_defaults(fn=cmd_dump)

    a = sub.add_parser("add", help="Add a host")
    a.add_argument("ip")
    a.add_argument("--label", default=None)
    a.add_argument("--note", default=None)
    a.add_argument("--disabled", action="store_true")
    a.add_argument("--push-key", action="store_true",
                   help="Also run ssh-copy-id to this host")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="Remove a host"); r.add_argument("ip"); r.set_defaults(fn=cmd_remove)
    e = sub.add_parser("enable",  help="Enable a host"); e.add_argument("ip"); e.set_defaults(fn=cmd_enable)
    d = sub.add_parser("disable", help="Disable a host"); d.add_argument("ip"); d.set_defaults(fn=cmd_disable)

    st = sub.add_parser("set-threshold", help="Set vm/host threshold percentage")
    st.add_argument("which", choices=["vm", "host"])
    st.add_argument("value", type=int)
    st.set_defaults(fn=cmd_set_threshold)

    s = sub.add_parser("simulate", help="Dry-run decision logic at a fake charge")
    s.add_argument("charge", type=int)
    s.add_argument("--on-battery", action="store_true",
                   help="Force on-battery status even at 100%")
    s.set_defaults(fn=cmd_simulate)

    t = sub.add_parser("test-ssh", help="SSH connectivity check")
    t.add_argument("ip", nargs="?")
    t.set_defaults(fn=cmd_test_ssh)

    sub.add_parser("reset-state", help="Reset the power_state file to NORMAL").set_defaults(fn=cmd_reset_state)

    pk = sub.add_parser("push-key", help="ssh-copy-id to a host"); pk.add_argument("ip"); pk.set_defaults(fn=cmd_push_key)

    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(args.fn(args))
