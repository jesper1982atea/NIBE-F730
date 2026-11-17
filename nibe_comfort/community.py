"""How many installations the project has, published by the maintainer as a small JSON file.

The file is built from GitHub's own clone statistics (unique cloners over 14 days). Home
Assistant clones/pulls the add-on repository when installing and checking for updates, so it
roughly counts active installations. Fetching it sends nothing about this installation.
"""

import json
import threading
import time
from urllib.request import Request, urlopen

CACHE_SECONDS = 12 * 3600
RETRY_SECONDS = 3600


class Community:
    def __init__(self, url, fetch=None, clock=time.time):
        self.url = url
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.value = None
        self.checked = None
        self.ok = False

    def _fetch(self, url):
        with urlopen(Request(url, headers={"User-Agent": "ha-addon"}), timeout=10) as response:
            return response.read(4096)

    def get(self):
        """{"installations": int, "updated": "YYYY-MM-DD"} or None; never raises."""
        with self.lock:
            now = self.clock()
            if self.checked is not None and now - self.checked < (CACHE_SECONDS if self.ok else RETRY_SECONDS):
                return self.value
            self.checked = now
            try:
                data = json.loads(self.fetch(self.url))
                count = data["installations_14d"]
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count < 1_000_000:
                    raise ValueError(count)
                updated = data.get("updated")
                self.value = {"installations": count, "updated": updated if isinstance(updated, str) else None}
                self.ok = True
            except Exception:  # offline, rate limited or malformed: keep the last good value
                self.ok = False
            return self.value
