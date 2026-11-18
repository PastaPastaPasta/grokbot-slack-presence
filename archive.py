import json
import os
import re
import sqlite3
import threading
import time

STATE_DIR = os.environ.get("STATE_DIRECTORY", "/var/lib/<name>").split(":")[0]
DB_PATH = os.path.join(STATE_DIR, "archive.db")
PLACEHOLDER = "REPLACE_ME"

REQUIRED = {
    "relay": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GROK_WEBHOOK_URL", "GROK_WEBHOOK_KEY", "SLACK_ALLOWED_USERS"],
    "mcp": ["SLACK_BOT_TOKEN"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  ts TEXT NOT NULL,
  thread_ts TEXT,
  user TEXT,
  bot_id TEXT,
  subtype TEXT,
  text TEXT NOT NULL DEFAULT '',
  reply_count INTEGER,
  files TEXT,
  edited INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(channel, ts)
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(channel, thread_ts);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(channel, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY,
  name TEXT,
  kind TEXT,
  is_member INTEGER,
  is_archived INTEGER,
  im_user TEXT,
  topic TEXT,
  purpose TEXT,
  num_members INTEGER,
  updated INTEGER
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  name TEXT,
  real_name TEXT,
  display_name TEXT,
  title TEXT,
  email TEXT,
  tz TEXT,
  is_bot INTEGER,
  deleted INTEGER,
  updated INTEGER
);
CREATE TABLE IF NOT EXISTS sync_state (
  channel TEXT PRIMARY KEY,
  recent_done_at INTEGER,
  full_done_at INTEGER,
  oldest_fetched TEXT,
  synced_at INTEGER
);
"""


def missing_env(service):
    return [k for k in REQUIRED[service] if os.environ.get(k, PLACEHOLDER).strip() in ("", PLACEHOLDER)]


def channel_kind(c):
    if c.get("is_im"):
        return "im"
    if c.get("is_mpim"):
        return "mpim"
    if c.get("is_private") or c.get("is_group"):
        return "private_channel"
    return "public_channel"


def user_display(u):
    p = u.get("profile", {}) or {}
    return p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or u.get("id")


class Archive:
    def __init__(self, path=DB_PATH):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(channels)")}
            if "bot_removed" not in cols:
                self.db.execute("ALTER TABLE channels ADD COLUMN bot_removed INTEGER NOT NULL DEFAULT 0")

    def _exec(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def _many(self, sql, rows):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany(sql, rows)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    @staticmethod
    def _row(channel, m):
        files = [f.get("name") or f.get("title") for f in m.get("files", []) if isinstance(f, dict)]
        return (
            channel, m["ts"], m.get("thread_ts"), m.get("user"), m.get("bot_id"), m.get("subtype"),
            m.get("text") or "", m.get("reply_count"), json.dumps(files) if files else None, 1 if "edited" in m else 0,
        )

    UPSERT = """INSERT INTO messages(channel, ts, thread_ts, user, bot_id, subtype, text, reply_count, files, edited)
      VALUES (?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(channel, ts) DO UPDATE SET thread_ts=excluded.thread_ts, user=COALESCE(excluded.user, user),
        bot_id=COALESCE(excluded.bot_id, bot_id), subtype=excluded.subtype, text=excluded.text,
        reply_count=COALESCE(excluded.reply_count, reply_count), files=COALESCE(excluded.files, files),
        edited=MAX(edited, excluded.edited)
      WHERE messages.deleted = 0"""

    def put_messages(self, channel, msgs):
        rows = [self._row(channel, m) for m in msgs if m.get("ts")]
        if rows:
            self._many(self.UPSERT, rows)

    def apply_event(self, e):
        channel, st = e.get("channel"), e.get("subtype")
        if not channel:
            return
        if st == "message_changed" and e.get("message"):
            self.put_messages(channel, [e["message"]])
        elif st == "message_deleted" and e.get("deleted_ts"):
            self._exec("UPDATE messages SET deleted=1, text='' WHERE channel=? AND ts=?", (channel, e["deleted_ts"]))
        elif st == "message_replied" and e.get("message"):
            self.put_messages(channel, [e["message"]])
        elif e.get("ts") and st not in ("message_changed", "message_deleted", "message_replied"):
            self.put_messages(channel, [e])

    def put_channels(self, chans):
        now = int(time.time())
        rows = [
            (c["id"], c.get("name") or c.get("name_normalized"), channel_kind(c), 1 if c.get("is_member") or c.get("is_im") else 0,
             1 if c.get("is_archived") else 0, c.get("user"), (c.get("topic") or {}).get("value"),
             (c.get("purpose") or {}).get("value"), c.get("num_members"), now)
            for c in chans
        ]
        self._many(
            """INSERT INTO channels(id, name, kind, is_member, is_archived, im_user, topic, purpose, num_members, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
               name=COALESCE(excluded.name, name), kind=excluded.kind, is_member=excluded.is_member,
               is_archived=excluded.is_archived, im_user=COALESCE(excluded.im_user, im_user),
               topic=COALESCE(excluded.topic, topic), purpose=COALESCE(excluded.purpose, purpose),
               num_members=COALESCE(excluded.num_members, num_members), updated=excluded.updated""",
            rows,
        )

    def membership(self):
        return {r["id"]: r for r in self.query("SELECT id, kind, is_member, bot_removed FROM channels")}

    def mark_removed(self, ids):
        self._many("UPDATE channels SET is_member=0, bot_removed=1 WHERE id=?", [(i,) for i in ids])

    def mark_not_member_except(self, listed):
        listed = set(listed)
        gone = [r["id"] for r in self.query("SELECT id FROM channels WHERE is_member=1 AND kind != 'im'") if r["id"] not in listed]
        self._many("UPDATE channels SET is_member=0 WHERE id=?", [(i,) for i in gone])

    def recent_threads(self, channel, since, limit=100):
        return [r["thread_ts"] for r in self.query(
            "SELECT DISTINCT thread_ts FROM messages WHERE channel=? AND thread_ts IS NOT NULL AND thread_ts != ts "
            "AND ts >= ? ORDER BY ts DESC LIMIT ?", (channel, f"{since:.6f}", limit))]

    def rename_channel(self, cid, name):
        self._exec("UPDATE channels SET name=?, updated=? WHERE id=?", (name, int(time.time()), cid))

    def put_users(self, users):
        now = int(time.time())
        rows = [
            (u["id"], u.get("name"), u.get("real_name") or (u.get("profile") or {}).get("real_name"),
             (u.get("profile") or {}).get("display_name"), (u.get("profile") or {}).get("title"),
             (u.get("profile") or {}).get("email"), u.get("tz"), 1 if u.get("is_bot") else 0, 1 if u.get("deleted") else 0, now)
            for u in users if u.get("id")
        ]
        self._many("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def user_name(self, uid):
        r = self.query("SELECT display_name, real_name, name FROM users WHERE id=?", (uid,))
        if not r:
            return None
        return r[0]["display_name"] or r[0]["real_name"] or r[0]["name"]

    def sync_state(self, channel):
        r = self.query("SELECT * FROM sync_state WHERE channel=?", (channel,))
        return r[0] if r else {"channel": channel, "recent_done_at": None, "full_done_at": None, "oldest_fetched": None, "synced_at": None}

    def update_sync(self, channel, **fields):
        cur = self.sync_state(channel)
        cur.update(fields)
        self._exec(
            "INSERT OR REPLACE INTO sync_state(channel, recent_done_at, full_done_at, oldest_fetched, synced_at) VALUES (?,?,?,?,?)",
            (channel, cur["recent_done_at"], cur["full_done_at"], cur["oldest_fetched"], cur["synced_at"]),
        )

    def thread_max_ts(self, channel, thread_ts):
        r = self.query("SELECT MAX(ts) AS m FROM messages WHERE channel=? AND thread_ts=?", (channel, thread_ts))
        return r[0]["m"]

    def history(self, channel, limit=50, oldest=None, latest=None):
        sql = "SELECT * FROM messages WHERE channel=? AND deleted=0 AND (thread_ts IS NULL OR thread_ts=ts OR subtype='thread_broadcast')"
        args = [channel]
        if oldest:
            sql += " AND CAST(ts AS REAL) >= ?"
            args.append(float(oldest))
        if latest:
            sql += " AND CAST(ts AS REAL) <= ?"
            args.append(float(latest))
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return self.query(sql, args)

    def thread(self, channel, thread_ts, limit=500):
        return self.query(
            "SELECT * FROM messages WHERE channel=? AND deleted=0 AND (ts=? OR thread_ts=?) ORDER BY ts LIMIT ?",
            (channel, thread_ts, thread_ts, limit),
        )

    def resolve_channel(self, ref):
        ref = ref.strip().lstrip("#")
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", ref):
            return ref
        r = self.query("SELECT id FROM channels WHERE name=? COLLATE NOCASE ORDER BY is_member DESC LIMIT 1", (ref,))
        return r[0]["id"] if r else ref

    def stats(self):
        r = self.query(
            "SELECT (SELECT COUNT(*) FROM messages) AS messages, (SELECT COUNT(*) FROM channels WHERE is_member=1) AS member_channels, "
            "(SELECT COUNT(*) FROM sync_state WHERE recent_done_at IS NOT NULL) AS recent_backfilled_channels, "
            "(SELECT COUNT(*) FROM sync_state WHERE full_done_at IS NOT NULL) AS full_backfilled_channels, (SELECT COUNT(*) FROM users) AS users"
        )
        return r[0]


def fts_query(q):
    terms = re.findall(r'"[^"]+"|\S+', q)
    out = []
    for t in terms:
        if t in ("OR", "AND", "NOT"):
            if out and out[-1] not in ("OR", "AND", "NOT"):
                out.append(t)
            continue
        prefix = t.endswith("*")
        t = t.strip('"*')
        t = t.replace('"', "")
        if t:
            out.append(f'"{t}"' + ("*" if prefix else ""))
    while out and out[-1] in ("OR", "AND", "NOT"):
        out.pop()
    while out and out[0] in ("OR", "AND", "NOT"):
        out.pop(0)
    return " ".join(out)
