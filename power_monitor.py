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
    if not dry_run:
        try:
            pmlib.write_heartbeat(cfg)  # read by the self-test and web watchdog
        except OSError as e:
            log.error(f"could not write heartbeat: {e}")

    try:
        ups = pmlib.read_ups(cfg["ups_name"])
    except Exception as e:
        log.error(f"Error reading UPS: {e}")
        return 1

    status = ups.get("ups.status", "")
    charge = ups["_charge"]
    state = pmlib.read_state(cfg)
    retry_ips = pmlib.read_guest_retry(cfg)
    action = pmlib.decide_action(cfg, status, charge, state,
                                 guest_retry_pending=bool(retry_ips))

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
        results = pmlib.shutdown_guests(cfg, dry_run=dry_run, log=log)
        failed = [r["host"] for r in results if not r["ok"]]
        if failed:
            log.warning(f"Guest shutdown failed on {', '.join(failed)}; "
                        f"will retry every minute until the host stage.")
        if not dry_run:
            pmlib.write_state(cfg, pmlib.GUESTS_DOWN, guest_retry=failed)
        pmlib.notify_event(cfg, "guests_shutdown", ups, dry_run=dry_run, log=log)
        return 0

    if action == "retry_guests":
        log.warning(f"Retrying guest shutdown on {', '.join(retry_ips)} "
                    f"(charge {charge}%, dry_run={dry_run})")
        results = pmlib.shutdown_guests(cfg, dry_run=dry_run, log=log,
                                        only_ips=retry_ips)
        # Hosts removed/disabled since the last attempt drop out naturally.
        still = [r["host"] for r in results if not r["ok"]]
        if still:
            log.warning(f"Guest shutdown still failing on {', '.join(still)}")
        else:
            log.info("Guest shutdown retry succeeded on all pending hosts.")
        if not dry_run:
            pmlib.write_state(cfg, pmlib.GUESTS_DOWN, guest_retry=still)
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


def scheduled_selftest() -> None:
    """Daily self-test, run *after* main() so it can never delay or break the
    shutdown decision for this tick. Skipped entirely under DRY_RUN."""
    if os.environ.get("DRY_RUN") == "1":
        return
    cfg = pmlib.load_config()
    log = pmlib.get_logger(cfg)
    try:
        pmlib.maybe_run_scheduled_selftest(cfg, log=log)
    except Exception as e:
        log.error(f"self-test crashed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    rc = main()
    scheduled_selftest()
    sys.exit(rc)
