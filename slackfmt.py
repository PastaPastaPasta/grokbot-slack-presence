import re

MAX_TEXT = 4000
SLACK_LINK = re.compile(
    r"https://[a-z0-9-]+\.slack\.com/archives/([CGD][A-Z0-9]+)/p(\d{10})(\d{6})(?:\?[^\s>|]*?thread_ts=(\d+\.\d+))?[^\s>|]*")


def blocks_text(blocks):
    out = []

    def walk(el):
        if isinstance(el, list):
            for x in el:
                walk(x)
            return
        if not isinstance(el, dict):
            return
        t = el.get("type")
        if t == "text":
            out.append(el.get("text", ""))
        elif t == "link":
            out.append(el.get("text") or el.get("url", ""))
        elif t == "user":
            out.append(f"<@{el.get('user_id')}>")
        elif t == "usergroup":
            out.append(f"<!subteam^{el.get('usergroup_id')}>")
        elif t == "channel":
            out.append(f"<#{el.get('channel_id')}>")
        elif t == "broadcast":
            out.append(f"<!{el.get('range')}>")
        elif t == "emoji":
            out.append(f":{el.get('name')}:")
        elif isinstance(el.get("text"), dict):
            out.append(el["text"].get("text", ""))
        for k in ("elements", "fields"):
            if k in el:
                walk(el[k])
        if t in ("rich_text_section", "rich_text_preformatted", "rich_text_quote", "section", "header"):
            out.append("\n")

    walk(blocks or [])
    return "".join(out).strip()


def file_meta(f):
    if not isinstance(f, dict):
        return f
    keep = ("id", "name", "title", "mimetype", "filetype", "size", "permalink", "url_private")
    return {k: f[k] for k in keep if f.get(k) is not None}


def message_text(m):
    return (m.get("text") or "").strip() or blocks_text(m.get("blocks"))


def shared_refs(m):
    """Shared/forwarded messages and quoted Slack message links found in a message, unresolved."""
    items, keys = [], set()
    for a in m.get("attachments") or []:
        mb = a.get("message_blocks") or []
        if not (a.get("is_share") or a.get("is_msg_unfurl") or mb):
            continue
        cid, ts = a.get("channel_id"), a.get("ts")
        if mb and not (cid and ts):
            cid, ts = mb[0].get("channel"), mb[0].get("ts")
        text = (a.get("text") or "").strip() or blocks_text(a.get("blocks")) \
            or blocks_text([b for x in mb for b in ((x.get("message") or {}).get("blocks") or [])]) or a.get("fallback") or ""
        item = {"source": "forwarded" if a.get("is_share") else "unfurl", "channel": cid, "ts": ts,
                "author_id": a.get("author_id"), "author_name": a.get("author_name") or a.get("author_subname"),
                "permalink": a.get("from_url") or a.get("original_url"), "text": text[:MAX_TEXT]}
        if a.get("files"):
            item["files"] = [file_meta(f) for f in a["files"]]
        items.append(item)
        keys.add((cid, ts))
    for mt in SLACK_LINK.finditer(m.get("text") or ""):
        cid, ts = mt[1], f"{mt[2]}.{mt[3]}"
        if (cid, ts) in keys:
            continue
        keys.add((cid, ts))
        items.append({"source": "link", "channel": cid, "ts": ts, "thread_ts": mt[4], "permalink": mt[0].replace("&amp;", "&"), "text": None})
    return items


def merge_original(item, orig):
    """Fill a shared ref from the original message fetched from Slack."""
    item["resolved"] = True
    item["text"] = message_text(orig)[:MAX_TEXT]
    if orig.get("user"):
        item["author_id"] = orig["user"]
    if orig.get("files"):
        item["files"] = [file_meta(f) for f in orig["files"]]
    for k in ("thread_ts", "reply_count"):
        if orig.get(k):
            item[k] = orig[k]
    nested = [r for r in shared_refs(orig) if r["source"] != "link"]
    if nested:
        item["shared"] = nested
    return item
