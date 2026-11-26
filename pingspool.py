"""Accepted pings, kept on disk until the webhook has taken them.

Without this the relay holds a ping only in memory: while it waits to merge follow-ups,
while it gathers context, and while it retries the webhook. A restart in that time, or a
webhook that stays down for longer than the three attempts, loses the ping without trace,
and the person who wrote it never gets an answer.

With it, every ping that passed the allowlist is written to <state dir>/ping-spool before
anything else happens, and removed when the webhook answers 2xx (or refuses it for good
with a 4xx). What is still there is delivered again: at start-up, and once a minute for
pings whose delivery failed. A ping older than PING_SPOOL_MINUTES (default 30) is dropped
with an error in the log. PING_SPOOL_MINUTES=0 turns the spool off.

A ping can therefore arrive twice: when the relay stops after the webhook took it and
before the file was removed. Redelivered pings carry `redelivered: true` in the payload so
the routine can check whether it already answered.

The files hold the Slack event as received, minus its action_token (a short-lived
credential, and useless after a restart), so a redelivered ping has no search_context.
"""
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("relay")

MINUTES = float(os.environ.get("PING_SPOOL_MINUTES", "30"))
RETRY_SECONDS = 60
REDELIVERED_NOTE = ("This ping was accepted earlier and is delivered again, after a restart or a failed delivery. It may "
                    "already have been handled: check bot_last_reply and the thread before you act or answer.")


def _key(event):
    return f"{event.get('channel')}-{event.get('ts')}"


def _without_token(value):
    if isinstance(value, dict):
        return {k: _without_token(v) for k, v in value.items() if k != "action_token"}
    if isinstance(value, list):
        return [_without_token(v) for v in value]
    return value


class PingSpool:
    def __init__(self, state_dir=None, minutes=MINUTES):
        state_dir = state_dir or os.environ.get("STATE_DIRECTORY", ".").split(":")[0]
        self.dir = os.path.join(state_dir, "ping-spool")
        self.max_age = minutes * 60
        self.enabled = minutes > 0
        self.lock = threading.Lock()
        self.busy = set()  # keys being delivered right now
        if self.enabled:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)

    def _path(self, key):
        return os.path.join(self.dir, re.sub(r"[^A-Za-z0-9._-]", "_", key) + ".json")

    def put(self, event):
        """A ping was accepted: keep it until settle() says it was taken. Never raises."""
        if not self.enabled:
            return
        key = _key(event)
        with self.lock:
            self.busy.add(key)
            path = self._path(key)
            if os.path.exists(path):  # a redelivery: keep the first arrival time
                return
            try:
                tmp = path + ".tmp"
                with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                    json.dump({"received": time.time(), "event": _without_token(event)}, f, default=str)
                os.replace(tmp, path)
            except OSError:
                log.exception("could not spool ping %s; it is only in memory", key)

    def settle(self, events, keep):
        """Delivery of these pings is over. keep=False: taken or refused for good, forget them. keep=True: try again later."""
        if not self.enabled:
            return
        with self.lock:
            for event in events:
                key = _key(event)
                self.busy.discard(key)
                if not keep:
                    try:
                        os.unlink(self._path(key))
                    except FileNotFoundError:
                        pass
                    except OSError:
                        log.exception("could not remove spooled ping %s", key)

    def due(self):
        """Batches to deliver again: spooled pings nobody is delivering, grouped by thread, oldest first.
        The pings are marked busy; the caller hands every batch to deliver(), which settles them."""
        if not self.enabled:
            return []
        groups, now = {}, time.time()
        with self.lock:
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"):
                    continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        item = json.load(f)
                    event, received = item["event"], float(item["received"])
                except (OSError, ValueError, KeyError, TypeError):
                    log.exception("unreadable spooled ping %s; removed", name)
                    os.unlink(path)
                    continue
                key = _key(event)
                if key in self.busy:
                    continue
                if now - received > self.max_age:
                    log.error("ping %s/%s was not delivered within %d minutes; dropped", event.get("channel"), event.get("ts"), self.max_age / 60)
                    os.unlink(path)
                    continue
                self.busy.add(key)
                event["_redelivered"] = True
                groups.setdefault((event.get("channel"), event.get("thread_ts") or event.get("ts")), []).append((received, event))
        batches = [[e for _, e in sorted(g, key=lambda x: float(x[1].get("ts") or 0))] for g in groups.values()]
        return sorted(batches, key=lambda b: float(b[0].get("ts") or 0))

    def waiting(self):
        if not self.enabled:
            return 0
        try:
            return sum(1 for n in os.listdir(self.dir) if n.endswith(".json"))
        except OSError:
            return 0
