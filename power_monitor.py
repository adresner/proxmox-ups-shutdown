#!/usr/bin/env python3
"""Cron worker: every minute, decide whether to shut down guests or hosts.

Set DRY_RUN=1 in the environment to log decisions without running SSH.
"""
import os
import sys

import pmlib


def main() -> int:
    cfg = pmlib.load_config()
    log = pmlib.get_logger(cfg)
    dry_run = os.environ.get("DRY_RUN") == "1"

    try:
        ups = pmlib.read_ups(cfg["ups_name"])
    except Exception as e:
        log.error(f"Error reading UPS: {e}")
        return 1

    status = ups.get("ups.status", "")
    charge = ups["_charge"]
    state = pmlib.read_state(cfg)

    action = pmlib.decide_action(cfg, status, charge, state)

    if action == "noop":
        # If we just transitioned to on-battery, log it once.
        if ups["_on_battery"] and state == pmlib.NORMAL:
            log.info(f"Power Failure Detected. On Battery. Charge: {charge}%")
        return 0

    if action == "reset_normal":
        log.info("Power Restored. Resetting state.")
        pmlib.write_state(cfg, pmlib.NORMAL)
        return 0

    if action == "shutdown_guests":
        log.warning(
            f"Battery {charge}% <= {cfg['thresholds']['vm_shutdown_pct']}%. "
            f"Shutting down GUESTS. (dry_run={dry_run})"
        )
        pmlib.shutdown_guests(cfg, dry_run=dry_run, log=log)
        if not dry_run:
            pmlib.write_state(cfg, pmlib.GUESTS_DOWN)
        return 0

    if action == "shutdown_hosts":
        log.critical(
            f"Battery {charge}% <= {cfg['thresholds']['host_shutdown_pct']}%. "
            f"Shutting down HOSTS. (dry_run={dry_run})"
        )
        pmlib.shutdown_hosts(cfg, dry_run=dry_run, log=log)
        if not dry_run:
            pmlib.write_state(cfg, pmlib.HOSTS_DOWN)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
