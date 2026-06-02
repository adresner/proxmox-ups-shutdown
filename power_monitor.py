#!/usr/bin/env python3
"""Cron worker: every minute, decide whether to shut down guests or hosts.

Also fires email notifications (if configured) for each state transition and
when the battery first dips below the warning threshold. Each event sends at
most once per outage thanks to the 'sent' bookkeeping in the state file; on
power restored, the sent list is cleared and the next outage gets a fresh
round of emails.

Set DRY_RUN=1 in the environment to log decisions and would-send emails
without running SSH or contacting SMTP.
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

    # Warning email — fires once per outage when battery first drops below
    # warning_pct, regardless of whether the next stage has been reached yet.
    notif = cfg.get("notifications", {})
    warn_pct = int(notif.get("warning_pct", 80) or 80)
    if (ups["_on_battery"] and charge <= warn_pct
            and charge > cfg["thresholds"]["vm_shutdown_pct"]):
        pmlib.notify_event(cfg, "warning", ups, dry_run=dry_run, log=log)

    if action == "noop":
        if ups["_on_battery"] and state == pmlib.NORMAL:
            log.info(f"Power Failure Detected. On Battery. Charge: {charge}%")
        return 0

    if action == "reset_normal":
        # Send power_restored email before resetting (since reset clears 'sent').
        pmlib.notify_event(cfg, "power_restored", ups, dry_run=dry_run, log=log)
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
        pmlib.notify_event(cfg, "guests_shutdown", ups, dry_run=dry_run, log=log)
        return 0

    if action == "shutdown_hosts":
        log.critical(
            f"Battery {charge}% <= {cfg['thresholds']['host_shutdown_pct']}%. "
            f"Shutting down HOSTS. (dry_run={dry_run})"
        )
        pmlib.shutdown_hosts(cfg, dry_run=dry_run, log=log)
        if not dry_run:
            pmlib.write_state(cfg, pmlib.HOSTS_DOWN)
        pmlib.notify_event(cfg, "hosts_shutdown", ups, dry_run=dry_run, log=log)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
