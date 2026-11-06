"""Explicit, content-scoped consent with separate anonymous/account engines."""
from dataclasses import replace
from contextlib import nullcontext
import threading

from desktop.models import DownloadOptions, parse_profile
from desktop.services.engine import EngineService
from desktop.services.errors import SessionExpired, classify_error
from desktop.services.pacing import Pacer, PacingConfig


class PublicFirstService:
    def __init__(self, sessions=None, account="", pacing=None, engine_factory=EngineService):
        self.sessions, self.account = sessions, account
        self.engine_factory = engine_factory
        self.pacing_config = pacing if pacing is not None else PacingConfig()
        self.pacer = Pacer(self.pacing_config)
        self.contexts = {}
        self.auth_handler = lambda request: None
        self.current_account = ""
        self.invalid_accounts = set()
        self.statistics = None
        self.safety = None
        self._execution = threading.Lock()

    def _engine(self, account):
        if account and account in self.invalid_accounts:
            raise SessionExpired()
        if account not in self.contexts:
            # Every EngineService creates its own requests Session and RateController.
            engine = self.engine_factory(self.sessions, account, pacing=self.pacing_config)
            engine.pacer = self.pacer
            engine.statistics = self.statistics
            engine.safety = self.safety
            self.contexts[account] = engine
        return self.contexts[account]

    def _forget(self, account):
        engine = self.contexts.pop(account, None)
        if engine:
            engine.close()

    def run(self, job, control, report):
        if not self._execution.acquire(blocking=False):
            raise RuntimeError("Only one profile may be active in a queue run.")
        try:
            return self._run(job, control, report)
        finally:
            self._execution.release()

    def _run(self, job, control, report):
        self.pacer.statistics = self.statistics
        if self.safety:
            self.safety.wait(control, report, self.statistics)
        job.options.validate()
        totals = {"files": 0, "items": 0}
        skipped, finished = [], []
        # Consent obtained for a private profile applies only to that profile's selected content.
        profile_account = self.account
        seen = set()
        options = job.options
        sections = []
        for label, fields in (("profile picture", ("profile_picture",)),
                              ("posts", ("photos", "videos")), ("Reels", ("reels",)),
                              ("Stories", ("stories",)), ("Highlights", ("highlights",))):
            if any(getattr(options, field) for field in fields):
                selected = DownloadOptions(**{key: key in fields and getattr(options, key)
                                             for key in ("photos", "videos", "reels", "stories", "highlights", "profile_picture")},
                                           captions=options.captions, metadata=False)
                sections.append((label, selected))

        def ask(label, reason, required=True):
            control.checkpoint(report)
            report("state", "Awaiting authentication")
            report("mode", "Public Mode — No Login")
            with self.statistics.measure("decision_seconds") if self.statistics else nullcontext():
                account = self.auth_handler({"username": job.username, "content": label,
                                             "reason": reason, "required": required})
            control.checkpoint(report)
            return parse_profile(account) if account else ""

        for index, (label, selected) in enumerate(sections):
            control.checkpoint(report)
            account = profile_account
            if label in ("Stories", "Highlights") and not account:
                account = ask(label, f"Instagram requires authentication for {label}. Your chosen account must already have access. You can skip {label} and keep other downloads.")
                if not account:
                    skipped.append(label)
                    continue

            def attempt(chosen):
                self.current_account = chosen
                base = dict(totals)
                def event(kind, value):
                    if kind in totals:
                        totals[kind] = base[kind] + value
                        report(kind, totals[kind])
                    else:
                        report(kind, value)
                report("mode", f"Checking session — @{chosen}" if chosen else "Public Mode — No Login")
                report("state", "Downloading")
                return self._engine(chosen).run(replace(job, options=selected), control, event, seen=seen)

            try:
                attempt(account)
            except Exception as error:
                failure = classify_error(error, authenticated=bool(account))
                private = failure.kind == "private_not_followed" and not account
                if failure.kind == "session_expired":
                    self.invalid_accounts.add(account)
                    self._forget(account)
                can_offer = private or (not account and failure.kind in ("authentication_required", "unavailable")) or failure.kind == "session_expired"
                if not can_offer:
                    raise
                reason = ("This profile is private. Login does not grant access: choose an account that already has permission. Your choice applies only to this profile's selected content."
                          if private else failure.message)
                if failure.kind == "unavailable":
                    reason += " You may optionally retry this content with login, or skip it."
                chosen = ask("private profile" if private else label, reason, failure.kind != "unavailable")
                if not chosen:
                    if private:
                        skipped.extend(name for name, _ in sections[index:])
                        break
                    skipped.append(label)
                    continue
                if failure.kind == "session_expired":
                    self._forget(chosen)  # Newly saved credentials must replace any cached context.
                self.invalid_accounts.discard(chosen)  # A new request requires this explicit approval.
                # One explicitly approved retry. A second failure never loops into more prompts.
                try:
                    if self.statistics:
                        self.statistics.add("retry_attempts")
                    with self.statistics.measure("retry_seconds") if self.statistics else nullcontext():
                        attempt(chosen)
                except Exception as retry_error:
                    if classify_error(retry_error, authenticated=True).kind == "session_expired":
                        self.invalid_accounts.add(chosen)
                        self._forget(chosen)
                        raise SessionExpired() from retry_error
                    raise
                if private:
                    profile_account = chosen
            finished.append(label)

        report("mode", "Public Mode — No Login")
        if skipped:
            return {"outcome": "Completed" if finished else "Skipped",
                    "detail": "Finished; skipped " + ", ".join(skipped) if finished else "Skipped " + ", ".join(skipped)}
        return None

    def close(self):
        for engine in self.contexts.values():
            engine.close()
        self.contexts.clear()
