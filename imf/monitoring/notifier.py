"""Event notification: application log, notification log file and (optionally) e-mail."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import NotificationCfg
from ..models import Event
from ..timeutil import fmt

log = logging.getLogger("imf.notifications")


class Notifier:
    def __init__(self, cfg: NotificationCfg, mission: str = ""):
        self.cfg = cfg
        self.mission = mission
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _wanted(self, severity: str) -> bool:
        if severity == "INFO":  # timeline events (e.g. calibration modes) are not notified
            return False
        return self.cfg.min_severity == "WARNING" or severity == "ALARM"

    def notify(self, events: list[Event], kind: str = "RAISED") -> None:
        lines = []
        for ev in events:
            if not self._wanted(ev.severity):
                continue
            p = ev.parameter
            when = ev.cleared_at if kind == "CLEARED" else ev.time
            if ev.limit_text and p is not None:
                code = p.code + (f"[{ev.dims}]" if ev.dims else "")
                line = (f"[{kind}] {ev.severity:7s} {code:16s} value={ev.value:.4g} {p.unit} "
                        f"limit {ev.limit_text} at {fmt(when, True)} (event #{ev.id})")
            else:
                line = f"[{kind}] {ev.severity:7s} {ev.rule or ev.category:16s} {ev.title} at {fmt(when, True)} (event #{ev.id})"
            if ev.recommended_action and kind == "RAISED":
                line += f"\n          ACTION: {ev.recommended_action}"
            lines.append(line)
        if not lines:
            return
        with open(self.cfg.log_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        for line in lines:
            log.warning(line)
        if self.cfg.smtp_host and self.cfg.recipients:
            self._send_mail(lines)

    def _send_mail(self, lines: list[str]) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[IMF {self.mission}] {len(lines)} monitoring event(s)"
        msg["From"] = self.cfg.sender
        msg["To"] = ", ".join(self.cfg.recipients)
        msg.set_content("\n".join(lines))
        try:
            with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=10) as smtp:
                smtp.send_message(msg)
        except OSError as exc:
            log.error("E-mail notification failed: %s", exc)
