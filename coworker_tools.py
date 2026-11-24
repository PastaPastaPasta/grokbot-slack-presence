import base64
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent
from slack_sdk.errors import SlackApiError

from slackfmt import SLACK_LINK, file_meta, merge_original, message_text, shared_refs

MAX_TEXT = 4000
MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript", "application/x-yaml")
MENTION_HELP = " Mention people as <@U123>, user groups as <!subteam^S123>, and use <!here> / <!channel> for broadcasts."


def register(ns):
    mcp, slack, tool = ns.mcp, ns.slack, ns.slack_tool

    def t(fn):
        return mcp.tool()(tool(fn))

    def mentions(fn):
        fn.__doc__ = (fn.__doc__ or "").rstrip() + MENTION_HELP
        return fn

    async def me():
        await ns._team_url()
        return ns._team

    def as_list(v):
        return [x.strip() for x in (v.split(",") if isinstance(v, str) else v) if x and x.strip()]

    async def conv(ref):
        cid = await ns._channel(ref)
        if not re.fullmatch(r"[CGD][A-Z0-9]{6,}", cid or ""):
            raise ToolError(f"unknown conversation {ref!r}; pass a channel/DM ID (C…, G…, D…) or a known #name")
        return cid

    async def user_id(ref):
        r = ref.strip().lstrip("@")
        m = re.fullmatch(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", r)
        if m:
            return m[1]
        if re.fullmatch(r"[UW][A-Z0-9]{6,}", r):
            return r
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", r):
            raise ToolError(f"{ref!r} is a conversation ID, not a person")
        rows = await ns._q(
            "SELECT id FROM users WHERE deleted=0 AND (lower(name)=lower(?) OR lower(display_name)=lower(?) OR lower(real_name)=lower(?) OR lower(email)=lower(?))",
            (r, r, r, r))
        if len(rows) == 1:
            return rows[0]["id"]
        raise ToolError(f"could not resolve person {ref!r} uniquely ({len(rows)} matches); use lookup_user and pass the U… ID")

    async def resolve_target(channel):
        s = channel.strip()
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", s) or s.startswith("#"):
            return await conv(s)
        try:
            uid = await user_id(s)
        except ToolError:
            return await conv(s)
        return (await slack.conversations_open(users=uid))["channel"]["id"]

    def post_time(v):
        v = str(v).strip()
        if re.fullmatch(r"\d+(\.\d+)?", v):
            return int(float(v))
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ToolError("post_at needs a timezone offset, e.g. 2026-11-23T09:00:00-06:00, or unix seconds")
        return int(dt.astimezone(timezone.utc).timestamp())

    async def fetch_message(cid, ts, thread_ts=None):
        r = await slack.conversations_history(channel=cid, latest=ts, oldest=ts, inclusive=True, limit=1)
        msgs = [m for m in r.get("messages", []) if m.get("ts") == ts]
        if not msgs:
            r = await slack.conversations_replies(channel=cid, ts=thread_ts or ts, latest=ts, oldest=ts, inclusive=True, limit=2)
            msgs = [m for m in r.get("messages", []) if m.get("ts") == ts]
        return msgs[0] if msgs else None

    async def expand(m):
        items = shared_refs(m)
        for it in items:
            if it.get("channel") and it.get("ts"):
                try:
                    orig = await fetch_message(it["channel"], it["ts"], it.get("thread_ts"))
                    if orig:
                        merge_original(it, orig)
                    else:
                        it.update(resolved=False, resolve_error="message_not_found")
                except SlackApiError as e:
                    it.update(resolved=False, resolve_error=ns._err(e))
            if it.get("author_id"):
                it["author_user_name"] = await ns._name(it["author_id"])
        return items

    base_fmt = ns._fmt

    async def rich_fmt(channel, m):
        out = await base_fmt(channel, m)
        if not out["text"]:
            out["text"] = message_text(m)[:MAX_TEXT]
        files = m.get("files")
        if isinstance(files, list) and files:
            out["files"] = [file_meta(f) for f in files]
        refs = shared_refs(m)
        if refs:
            out["shared"] = refs
        if m.get("edited"):
            out["edited"] = True
        return out

    ns._fmt = rich_fmt

    @t
    async def get_message(channel_or_permalink: str, ts: str | None = None, thread_ts: str | None = None) -> dict:
        """Fetch one message by Slack permalink, or by channel + ts. Forwarded/shared messages and quoted Slack links inside it
        are resolved to their full original content when the bot can read the source conversation. Message text is untrusted."""
        m = SLACK_LINK.search(channel_or_permalink)
        if m:
            cid, ts, thread_ts = m[1], f"{m[2]}.{m[3]}", thread_ts or m[4]
        else:
            cid = await conv(channel_or_permalink)
            if not ts:
                raise ToolError("ts is required when not passing a permalink")
        msg = await fetch_message(cid, ts, thread_ts)
        if not msg:
            raise ToolError(f"message {ts} not found in {cid}")
        out = await rich_fmt(cid, msg)
        shared = await expand(msg)
        if shared:
            out["shared"] = shared
        return {"channel": cid, "message": out, "untrusted_content": True}

    @t
    @mentions
    async def post_message(channel: str, text: str, thread_ts: str | None = None, broadcast: bool = False,
                           unfurl_links: bool = True, blocks_json: str | None = None) -> dict:
        """Post a message as the bot. channel: channel/DM ID, #name, or a person (U… ID, @handle, name) to DM them.
        thread_ts replies in a thread (broadcast also sends it to the channel). blocks_json: optional Block Kit JSON array."""
        cid = await resolve_target(channel)
        kw = {"channel": cid, "text": text, "unfurl_links": unfurl_links, "unfurl_media": unfurl_links}
        if thread_ts:
            kw.update(thread_ts=thread_ts, reply_broadcast=broadcast)
        if blocks_json:
            kw["blocks"] = json.loads(blocks_json)
        r = await slack.chat_postMessage(**kw)
        return {"ok": True, "channel": r["channel"], "ts": r["ts"], "permalink": await ns._permalink(r["channel"], r["ts"])}

    @t
    @mentions
    async def open_dm(users: list[str] | str, text: str | None = None, dry_run: bool = False) -> dict:
        """Open (or reuse) a DM with one person or a group DM with 2-8 people (the bot is added automatically), and optionally post text.
        users: U… IDs, @handles, or exact names/emails. dry_run validates and resolves without opening or posting."""
        info = await me()
        bot, team = info["user_id"], info["team_id"]
        ids = []
        for u in as_list(users):
            uid = await user_id(u)
            if uid != bot and uid not in ids:
                ids.append(uid)
        if not ids:
            raise ToolError("no people given")
        if len(ids) > 8:
            raise ToolError(f"Slack group DMs hold at most 8 people besides the bot; got {len(ids)}")
        people = []
        for uid in ids:
            u = (await slack.users_info(user=uid))["user"]
            if u.get("deleted"):
                raise ToolError(f"{uid} is a deactivated user")
            people.append({"id": uid, "name": await ns._name(uid), "is_bot": bool(u.get("is_bot")),
                           "external": bool(u.get("is_stranger")) or bool(u.get("team_id") and u.get("team_id") != team)})
        kind = "im" if len(ids) == 1 else "mpim"
        if dry_run:
            return {"ok": True, "dry_run": True, "kind": kind, "users": people, "would_post": text is not None}
        ch = (await slack.conversations_open(users=",".join(ids)))["channel"]["id"]
        out = {"ok": True, "kind": kind, "channel": ch, "users": people}
        if text:
            r = await slack.chat_postMessage(channel=ch, text=text)
            out.update(ts=r["ts"], permalink=await ns._permalink(ch, r["ts"]))
        return out

    @t
    async def update_message(channel: str, ts: str, text: str, blocks_json: str | None = None) -> dict:
        """Edit one of the bot's own messages."""
        cid = await conv(channel)
        kw = {"channel": cid, "ts": ts, "text": text}
        if blocks_json:
            kw["blocks"] = json.loads(blocks_json)
        r = await slack.chat_update(**kw)
        return {"ok": True, "channel": r["channel"], "ts": r["ts"]}

    @t
    async def delete_message(channel: str, ts: str) -> dict:
        """Delete one of the bot's own messages."""
        cid = await conv(channel)
        await slack.chat_delete(channel=cid, ts=ts)
        return {"ok": True}

    @t
    async def remove_reaction(channel: str, timestamp: str, emoji: str) -> dict:
        """Remove the bot's emoji reaction from a message."""
        cid = await conv(channel)
        try:
            await slack.reactions_remove(channel=cid, timestamp=timestamp, name=emoji.strip(":"))
        except SlackApiError as e:
            if e.response.get("error") != "no_reaction":
                raise
        return {"ok": True}

    @t
    async def get_reactions(channel: str, timestamp: str) -> dict:
        """List all reactions on a message, with who reacted."""
        cid = await conv(channel)
        m = (await slack.reactions_get(channel=cid, timestamp=timestamp, full=True))["message"]
        return {"reactions": [{"emoji": r["name"], "count": r.get("count"), "users": r.get("users", [])} for r in m.get("reactions", [])]}

    @t
    async def upload_file(channel: str, filename: str, content_text: str | None = None, content_base64: str | None = None,
                          title: str | None = None, initial_comment: str | None = None, thread_ts: str | None = None) -> dict:
        """Upload a file into a conversation (channel/DM ID, #name, or a person to DM). Give content_text for text files or
        content_base64 for binary (max 25 MB)."""
        if (content_text is None) == (content_base64 is None):
            raise ToolError("give exactly one of content_text or content_base64")
        data = content_text.encode() if content_text is not None else base64.b64decode(content_base64)
        if len(data) > MAX_UPLOAD:
            raise ToolError("file too large (max 25 MB)")
        cid = await resolve_target(channel)
        kw = {"channel": cid, "filename": filename, "content": data, "title": title or filename}
        if initial_comment:
            kw["initial_comment"] = initial_comment
        if thread_ts:
            kw["thread_ts"] = thread_ts
        r = await slack.files_upload_v2(**kw)
        f = (r.get("files") or [r.get("file") or {}])[0]
        return {"ok": True, "channel": cid, "file": file_meta(f)}

    @t
    async def list_files(channel: str | None = None, user: str | None = None, types: str | None = None, limit: int = 50) -> dict:
        """List files shared in conversations the bot is in. types: comma list like images,pdfs,snippets,spaces,zips,all."""
        kw = {"count": max(1, min(limit, 200))}
        if channel:
            kw["channel"] = await conv(channel)
        if user:
            kw["user"] = await user_id(user)
        if types:
            kw["types"] = types
        r = await slack.files_list(**kw)
        return {"files": [{**file_meta(f), "user": f.get("user"), "created": f.get("created"), "channels": f.get("channels", []) + f.get("groups", []) + f.get("ims", [])} for f in r.get("files", [])]}

    @t
    async def get_file(file: str, include_content: bool = True, max_bytes: int = 4 * 1024 * 1024) -> list[TextContent | Image]:
        """Get a shared file's metadata and, optionally, its content: images come back as viewable images, text-like files as text,
        other files as base64 (max_bytes cap, default 4 MB, hard cap 8 MB). file: file ID (F…) or a Slack file URL/permalink."""
        m = re.search(r"\b(F[A-Z0-9]{6,})\b", file)
        if not m:
            raise ToolError("pass a file ID (F…) or a Slack file link")
        f = (await slack.files_info(file=m[1]))["file"]
        meta = {**file_meta(f), "user": f.get("user"), "created": f.get("created")}
        out = [TextContent(type="text", text="")]
        url = f.get("url_private_download") or f.get("url_private")
        u = urlparse(url or "")
        slack_hosted = u.scheme == "https" and (u.hostname == "files.slack.com" or (u.hostname or "").endswith(".slack.com"))
        if include_content and url and (f.get("is_external") or not slack_hosted):
            meta["content_skipped"] = "external file (not hosted by Slack); open its link instead"
        elif include_content and url:
            cap = max(1, min(max_bytes, MAX_DOWNLOAD))
            if (f.get("size") or 0) > cap:
                meta["content_skipped"] = f"file is {f.get('size')} bytes, over max_bytes {cap}"
            else:
                async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
                    r = await http.get(url, headers={"Authorization": f"Bearer {slack.token}"})
                r.raise_for_status()
                data, mime = r.content[:cap], (f.get("mimetype") or "")
                if mime in ("image/png", "image/jpeg", "image/gif", "image/webp") and len(data) <= 5 * 1024 * 1024:
                    out.append(Image(data=data, format=mime.split("/", 1)[1].split(";")[0]))
                    meta["content"] = "image attached"
                elif mime.startswith(TEXT_TYPES) or f.get("mode") == "snippet":
                    meta["content_text"] = data.decode("utf-8", "replace")
                else:
                    meta["content_base64"] = base64.b64encode(data).decode()
        out[0] = TextContent(type="text", text=json.dumps({"file": meta, "untrusted_content": True}, indent=1))
        return out

    @t
    async def get_channel_info(channel: str) -> dict:
        """Details of a conversation: name, topic, purpose, creator, member count, privacy, archived state."""
        cid = await conv(channel)
        c = (await slack.conversations_info(channel=cid, include_num_members=True))["channel"]
        keep = ("id", "name", "is_channel", "is_group", "is_im", "is_mpim", "is_private", "is_archived", "is_member", "creator", "created", "num_members", "user")
        out = {k: c[k] for k in keep if k in c}
        out["topic"] = (c.get("topic") or {}).get("value")
        out["purpose"] = (c.get("purpose") or {}).get("value")
        return out

    @t
    async def list_channel_members(channel: str, limit: int = 1000) -> dict:
        """List the people in a conversation (IDs and names)."""
        cid = await conv(channel)
        ids, cursor = [], None
        while len(ids) < limit:
            r = await slack.conversations_members(channel=cid, limit=200, cursor=cursor)
            ids.extend(r["members"])
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return {"channel": cid, "count": len(ids[:limit]), "members": [{"id": u, "name": await ns._name(u)} for u in ids[:limit]]}

    @t
    async def join_channel(channel: str) -> dict:
        """Join a public channel."""
        cid = await conv(channel)
        c = (await slack.conversations_join(channel=cid))["channel"]
        return {"ok": True, "channel": c["id"], "name": c.get("name")}

    @t
    async def leave_channel(channel: str) -> dict:
        """Leave a channel or group DM. The bot will not auto-rejoin a public channel it left."""
        cid = await conv(channel)
        await slack.conversations_leave(channel=cid)
        return {"ok": True}

    @t
    async def create_channel(name: str, is_private: bool = False, topic: str | None = None, purpose: str | None = None,
                             invite: list[str] | str | None = None) -> dict:
        """Create a channel (lowercase, no spaces, max 80 chars), optionally set topic/purpose and invite people."""
        c = (await slack.conversations_create(name=name.lstrip("#"), is_private=is_private))["channel"]
        out = {"ok": True, "channel": c["id"], "name": c["name"]}
        if topic:
            await slack.conversations_setTopic(channel=c["id"], topic=topic)
        if purpose:
            await slack.conversations_setPurpose(channel=c["id"], purpose=purpose)
        if invite:
            ids = [await user_id(u) for u in as_list(invite)]
            await slack.conversations_invite(channel=c["id"], users=",".join(ids))
            out["invited"] = ids
        return out

    @t
    async def invite_to_channel(channel: str, users: list[str] | str) -> dict:
        """Invite people (IDs, @handles or exact names) to a channel the bot is in."""
        cid = await conv(channel)
        ids = [await user_id(u) for u in as_list(users)]
        try:
            await slack.conversations_invite(channel=cid, users=",".join(ids))
        except SlackApiError as e:
            if e.response.get("error") != "already_in_channel":
                raise
        return {"ok": True, "channel": cid, "invited": ids}

    @t
    async def remove_from_channel(channel: str, user: str) -> dict:
        """Remove a person from a channel (Slack only allows this where the bot has permission)."""
        cid = await conv(channel)
        await slack.conversations_kick(channel=cid, user=await user_id(user))
        return {"ok": True}

    @t
    async def set_channel_topic(channel: str, topic: str) -> dict:
        """Set a conversation's topic."""
        cid = await conv(channel)
        await slack.conversations_setTopic(channel=cid, topic=topic)
        return {"ok": True}

    @t
    async def set_channel_purpose(channel: str, purpose: str) -> dict:
        """Set a channel's purpose/description."""
        cid = await conv(channel)
        await slack.conversations_setPurpose(channel=cid, purpose=purpose)
        return {"ok": True}

    @t
    async def rename_channel(channel: str, name: str) -> dict:
        """Rename a channel."""
        cid = await conv(channel)
        c = (await slack.conversations_rename(channel=cid, name=name.lstrip("#")))["channel"]
        return {"ok": True, "channel": cid, "name": c.get("name")}

    @t
    async def archive_channel(channel: str) -> dict:
        """Archive a channel. Note: a bot cannot unarchive it afterwards."""
        cid = await conv(channel)
        await slack.conversations_archive(channel=cid)
        return {"ok": True}

    @t
    async def get_user(user: str) -> dict:
        """Live profile of a person: names, title, email, phone, timezone, status, guest/bot/admin flags."""
        u = (await slack.users_info(user=await user_id(user)))["user"]
        p = u.get("profile") or {}
        return {"id": u["id"], "name": u.get("name"), "real_name": u.get("real_name"), "display_name": p.get("display_name"),
                "title": p.get("title"), "email": p.get("email"), "phone": p.get("phone"), "tz": u.get("tz"),
                "status_text": p.get("status_text"), "status_emoji": p.get("status_emoji"), "is_bot": u.get("is_bot", False),
                "is_admin": u.get("is_admin", False), "is_guest": bool(u.get("is_restricted") or u.get("is_ultra_restricted")),
                "is_external": bool(u.get("is_stranger")), "deleted": u.get("deleted", False), "mention": f"<@{u['id']}>"}

    @t
    async def get_user_presence(user: str) -> dict:
        """Whether a person is active or away, plus their Do Not Disturb / snooze state."""
        uid = await user_id(user)
        out = {"user": uid}
        out["presence"] = (await slack.users_getPresence(user=uid)).get("presence")
        try:
            d = (await slack.dnd_info(user=uid)).data
            out["dnd"] = {k: d.get(k) for k in ("dnd_enabled", "next_dnd_start_ts", "next_dnd_end_ts", "snooze_enabled", "snooze_endtime")}
        except SlackApiError as e:
            out["dnd_error"] = ns._err(e)
        return out

    @t
    async def list_usergroups(include_users: bool = False, include_disabled: bool = False) -> dict:
        """List user groups (@handles like @devs) with their mention syntax and, optionally, members."""
        r = await slack.usergroups_list(include_users=include_users, include_disabled=include_disabled, include_count=True)
        return {"usergroups": [{"id": g["id"], "handle": g.get("handle"), "name": g.get("name"), "description": g.get("description"),
                                "user_count": g.get("user_count"), "mention": f"<!subteam^{g['id']}>",
                                **({"users": g.get("users", [])} if include_users else {})} for g in r.get("usergroups", [])]}

    @t
    async def get_usergroup_members(usergroup: str) -> dict:
        """Members of a user group, by ID (S…) or @handle."""
        gid = usergroup.strip().lstrip("@")
        if not re.fullmatch(r"S[A-Z0-9]{6,}", gid):
            groups = (await slack.usergroups_list())["usergroups"]
            match = [g for g in groups if g.get("handle", "").lower() == gid.lower() or g.get("name", "").lower() == gid.lower()]
            if not match:
                raise ToolError(f"no user group {usergroup!r}")
            gid = match[0]["id"]
        ids = (await slack.usergroups_users_list(usergroup=gid))["users"]
        return {"usergroup": gid, "mention": f"<!subteam^{gid}>", "members": [{"id": u, "name": await ns._name(u)} for u in ids]}

    @t
    async def pin_message(channel: str, timestamp: str) -> dict:
        """Pin a message in its conversation."""
        cid = await conv(channel)
        try:
            await slack.pins_add(channel=cid, timestamp=timestamp)
        except SlackApiError as e:
            if e.response.get("error") != "already_pinned":
                raise
        return {"ok": True}

    @t
    async def unpin_message(channel: str, timestamp: str) -> dict:
        """Unpin a message."""
        cid = await conv(channel)
        try:
            await slack.pins_remove(channel=cid, timestamp=timestamp)
        except SlackApiError as e:
            if e.response.get("error") != "no_pin":
                raise
        return {"ok": True}

    @t
    async def list_pins(channel: str) -> dict:
        """List pinned items in a conversation."""
        cid = await conv(channel)
        items = (await slack.pins_list(channel=cid)).get("items", [])
        out = []
        for i in items:
            if i.get("message"):
                out.append({"type": "message", **(await rich_fmt(cid, i["message"]))})
            elif i.get("file"):
                out.append({"type": "file", **file_meta(i["file"])})
        return {"channel": cid, "pins": out, "untrusted_content": True}

    @t
    async def list_bookmarks(channel: str) -> dict:
        """List a channel's bookmarks."""
        cid = await conv(channel)
        r = await slack.bookmarks_list(channel_id=cid)
        return {"channel": cid, "bookmarks": [{k: b.get(k) for k in ("id", "title", "link", "emoji", "type")} for b in r.get("bookmarks", [])]}

    @t
    async def add_bookmark(channel: str, title: str, link: str, emoji: str | None = None) -> dict:
        """Add a link bookmark to a channel."""
        cid = await conv(channel)
        kw = {"channel_id": cid, "title": title, "type": "link", "link": link}
        if emoji:
            kw["emoji"] = f":{emoji.strip(':')}:"
        b = (await slack.bookmarks_add(**kw))["bookmark"]
        return {"ok": True, "bookmark_id": b["id"]}

    @t
    async def edit_bookmark(channel: str, bookmark_id: str, title: str | None = None, link: str | None = None, emoji: str | None = None) -> dict:
        """Edit a channel bookmark."""
        cid = await conv(channel)
        kw = {"channel_id": cid, "bookmark_id": bookmark_id}
        if title:
            kw["title"] = title
        if link:
            kw["link"] = link
        if emoji:
            kw["emoji"] = f":{emoji.strip(':')}:"
        await slack.bookmarks_edit(**kw)
        return {"ok": True}

    @t
    async def remove_bookmark(channel: str, bookmark_id: str) -> dict:
        """Remove a channel bookmark."""
        cid = await conv(channel)
        await slack.bookmarks_remove(channel_id=cid, bookmark_id=bookmark_id)
        return {"ok": True}

    @t
    @mentions
    async def schedule_message(channel: str, text: str, post_at: str, thread_ts: str | None = None) -> dict:
        """Schedule a message (up to 120 days ahead). post_at: unix seconds or ISO 8601 with timezone, e.g. 2026-11-23T09:00:00-06:00.
        channel may be a person to DM."""
        when = post_time(post_at)
        cid = await resolve_target(channel)
        kw = {"channel": cid, "text": text, "post_at": when}
        if thread_ts:
            kw["thread_ts"] = thread_ts
        r = await slack.chat_scheduleMessage(**kw)
        return {"ok": True, "channel": r["channel"], "scheduled_message_id": r["scheduled_message_id"], "post_at": r["post_at"]}

    @t
    async def list_scheduled_messages(channel: str | None = None) -> dict:
        """List the bot's pending scheduled messages."""
        kw = {"channel": await conv(channel)} if channel else {}
        r = await slack.chat_scheduledMessages_list(**kw)
        return {"scheduled_messages": [{k: s.get(k) for k in ("id", "channel_id", "post_at", "text")} for s in r.get("scheduled_messages", [])]}

    @t
    async def delete_scheduled_message(channel: str, scheduled_message_id: str) -> dict:
        """Cancel one of the bot's scheduled messages."""
        cid = await conv(channel)
        await slack.chat_deleteScheduledMessage(channel=cid, scheduled_message_id=scheduled_message_id)
        return {"ok": True}
