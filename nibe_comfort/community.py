"""Anonymous installation count.

When "Skicka anonym användningsstatistik" is on (the default), NIBE Comfort sends
{"app", "id", "version"} to the counter every 6 hours. `id` is a random value made on first
start and stored in /data; nothing about the home, the heat pump, temperatures or prices is sent. Turning
the setting off sends one "forget" so the id is removed from the counter.

The counter (a small Cloudflare Worker, source in github.com/peterwreiber/golvvarme-comfort under stats-worker/) answers
how many installations were active in the last 7 days, which is shown in the header.
"""

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

LOG = logging.getLogger("nibe_comfort")
APP = "nibe_comfort"
DEFAULT_URL = "https://stats.ha-comfort.workers.dev"
CACHE_SECONDS = 3600
RETRY_SECONDS = 600
PING_SECONDS = 6 * 3600
PING_RETRY_SECONDS = 3600


def _request(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"User-Agent": "ha-addon", **({"Content-Type": "application/json"} if data else {})}
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=10) as response:
        return response.read(4096)


class Community:
    """Reads the count; an empty `base` disables it (tests, dry runs)."""

    def __init__(self, base, fetch=None, clock=time.time):
        self.base = (base or "").rstrip("/")
        self.fetch = fetch or (lambda url: _request("GET", url))
        self.clock = clock
        self.lock = threading.Lock()
        self.value = None
        self.checked = None
        self.ok = False

    def get(self):
        """{"installations": int, "total": int, "updated": str|None} or None; never raises."""
        if not self.base:
            return None
        with self.lock:
            now = self.clock()
            if self.checked is not None and now - self.checked < (CACHE_SECONDS if self.ok else RETRY_SECONDS):
                return self.value
            self.checked = now
            try:
                data = json.loads(self.fetch(f"{self.base}/v1/stats?app={APP}"))
                count = data["active_7d"]
                total = data.get("total", count)
                for n in (count, total):
                    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < 10_000_000:
                        raise ValueError(n)
                updated = data.get("updated")
                self.value = {"installations": count, "total": total, "updated": updated if isinstance(updated, str) else None}
                self.ok = True
            except Exception:  # offline, rate limited or malformed: keep the last good value
                self.ok = False
            return self.value


class Reporter:
    """Sends the anonymous ping. Call tick(sharing) regularly (every minute is fine)."""

    def __init__(self, base, version, data_dir, post=None, clock=time.time):
        self.base = (base or "").rstrip("/")
        self.version = version if isinstance(version, str) and version else "dev"
        self.path = Path(data_dir) / "usage.json"
        self.post = post or (lambda url, body: _request("POST", url, body))
        self.clock = clock
        self.next_at = 0.0
        self.forget_at = 0.0
        self.state = self._load()

    def _load(self):
        try:
            state = json.loads(self.path.read_text())
            ident = state.get("id") if isinstance(state, dict) else None
            if isinstance(ident, str) and len(ident) == 32 and all(c in "0123456789abcdef" for c in ident):
                return {"id": ident, "registered": state.get("registered") is True}
        except (OSError, ValueError):
            pass
        return {"id": secrets.token_hex(16), "registered": False}

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.state))
        except OSError as exc:
            LOG.warning("Kunde inte spara id för användningsstatistiken: %s", exc)

    def tick(self, sharing):
        """Ping when due while sharing; after sharing is turned off, ask the counter to forget the id once."""
        if not self.base:
            return
        now = self.clock()
        if not sharing:
            if self.state["registered"] and now >= self.forget_at:
                try:
                    self.post(f"{self.base}/v1/forget", {"app": APP, "id": self.state["id"]})
                    self.state["registered"] = False
                    self._save()
                    self.next_at = self.forget_at = 0.0
                except Exception as exc:
                    LOG.info("Kunde inte ta bort installationen ur statistiken, försöker igen om en timme: %s", exc)
                    self.forget_at = now + PING_RETRY_SECONDS
            return
        if now < self.next_at:
            return
        try:
            self.post(f"{self.base}/v1/ping", {"app": APP, "id": self.state["id"], "version": self.version})
            self.state["registered"] = True
            self._save()
            self.next_at = now + PING_SECONDS
        except Exception:
            self.next_at = now + PING_RETRY_SECONDS
