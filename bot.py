#!/usr/bin/env python3
"""Posts new items from RSS/Atom feeds (F1 YouTube, F1.com, Motorsport.com...) to a Telegram channel.

No extra packages needed. Runs on GitHub Actions and keeps watching the feeds.
Settings come from environment variables:
  TELEGRAM_BOT_TOKEN  - token from @BotFather
  TELEGRAM_CHAT_ID    - e.g. @mychannel (public) or -100123456789 (private)
  DRY_RUN=1           - print what would be sent instead of sending
"""
import html
import json
import os
import http.client
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FEEDS_FILE = "feeds.txt"
STATE_FILE = "seen.json"
MAX_POSTS_PER_RUN = 8        # safety limit so you never get flooded
KEEP_SEEN = 6000             # how many old keys to remember (2 per post: link + title)
UA = {"User-Agent": "Mozilla/5.0 (compatible; telegram-feed-bot/1.0)"}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0"))   # how long to keep watching (0 = check once)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))   # how often to check
SIGNATURE = "🏁 @Live_F1 🏁"   # line shown at the bottom of every post


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local(tag):
    return tag.rsplit("}", 1)[-1]


TRACKING = {"fbclid", "gclid", "ref", "ref_src", "source", "cmp", "ocid", "mc_cid", "mc_eid",
            "feature", "si", "pp", "spm", "campaign", "mbid", "xtor", "ito", "rss"}


def clean_link(url):
    """Remove tracking junk (utm_..., fbclid...) from a link."""
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def link_key(url):
    """Same article = same key, however the link is spelled (www, http/https, trailing /, tracking)."""
    if url.startswith(("yt:", "u:", "t:")):      # already a key
        return url
    p = urllib.parse.urlsplit(clean_link(url))
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"):
        vid = dict(urllib.parse.parse_qsl(p.query)).get("v") or (p.path.strip("/") if host == "youtu.be" else "")
        if not vid and p.path.startswith(("/shorts/", "/live/")):
            vid = p.path.split("/")[2] if p.path.count("/") >= 2 else ""
        if vid:
            return "yt:" + vid
    q = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(p.query)))
    path = re.sub(r"/+$", "", p.path)
    return "u:" + host + path + ("?" + q if q else "")


def title_key(title, link):
    """Catches the same story published under different links (e.g. Autosport vs Motorsport.com)."""
    if link_key(link).startswith("yt:"):
        return None                                # video titles are checked by video id only
    t = re.sub(r"[^a-z0-9]+", "", title.lower())
    return "t:" + t if len(t) >= 20 else None


def keys_for(title, link):
    return [k for k in (link_key(link), title_key(title, link)) if k]


def resolve_youtube(handle):
    """Turn @formula1 into the channel's hidden RSS feed address."""
    page = fetch(f"https://www.youtube.com/{handle}").decode("utf-8", "ignore")
    m = re.search(r'"channelId":"(UC[\w-]{22})"', page) or re.search(
        r'channel_id=(UC[\w-]{22})', page)
    if not m:
        raise RuntimeError(f"Could not find channel id for {handle}")
    return m.group(1)


def youtube_items(channel_id):
    """Videos from a channel WITHOUT Shorts.
    YouTube has hidden playlists per content type: UULF... = normal videos, UULV... = live streams
    (Shorts are UUSH..., which we simply never read)."""
    base = "https://www.youtube.com/feeds/videos.xml?playlist_id="
    items, ok = [], False
    for prefix in ("UULF", "UULV"):
        try:
            items += parse_feed(fetch(base + prefix + channel_id[2:]))
            ok = True
        except Exception as e:
            if prefix == "UULF":        # a channel with no live streams legitimately has no UULV feed
                print(f"[warn] filtered YouTube feed failed ({e})")
    if not ok:                          # YouTube changed something: fall back to the normal feed
        print("[warn] using the unfiltered YouTube feed; Shorts are removed by link instead")
        items = parse_feed(fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"))
    return items


def load_sources():
    """feeds.txt lines look like:  Label | address   (address may be youtube:@handle)"""
    sources = []
    with open(FEEDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label, _, addr = line.partition("|")
            sources.append((label.strip(), addr.strip()))
    return sources


def parse_feed(data):
    """Return a list of (title, link) from an RSS or Atom feed, newest first as in the feed."""
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title, link = "", ""
        for child in el:
            name = local(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                href = child.attrib.get("href")
                if href:
                    if child.attrib.get("rel", "alternate") == "alternate" or not link:
                        link = href
                elif child.text and not link:
                    link = child.text
        if title and link:
            items.append((html.unescape(title), clean_link(link)))
    return items


def send(label, title, link):
    # Layout: title, link, empty line, channel signature (plain text, like the IFTTT version)
    text = f"{title}\n{link}\n\n{SIGNATURE}"
    if DRY_RUN:
        print("WOULD SEND:\n" + text + "\n")
        return True
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text,
        # Web page preview ON, always built from the video/article link
        "link_preview_options": json.dumps({"is_disabled": False, "url": link}),
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=payload),
            timeout=30)
        return True
    except urllib.error.HTTPError as e:
        print(f"Telegram error {e.code}: {e.read().decode('utf-8', 'ignore')}")
        if e.code == 429:
            time.sleep(10)
        return False
    except urllib.error.URLError as e:
        print(f"Could not reach Telegram (nothing was sent): {e}")
        return False
    except (OSError, http.client.HTTPException) as e:
        # The request went out but the answer got lost (timeout/reset). Telegram has most likely
        # delivered the post, and retrying would create a duplicate, so count it as sent.
        print(f"No reply from Telegram ({e}); assuming it was delivered.")
        return True


_yt_cache = {}
_short_cache = {}     # video id -> True/False, so each video is only checked once
_short_fails = {}     # video id -> how many times the check failed

SHORTS_URL = "https://www.youtube.com/shorts/{}"
SHORTS_RETRIES = 5    # if YouTube can't be asked, wait this many passes before posting anyway


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def is_short(video_id):
    """YouTube Shorts open at /shorts/<id>; normal videos get redirected to /watch?v=<id>."""
    if video_id in _short_cache:
        return _short_cache[video_id]
    req = urllib.request.Request(SHORTS_URL.format(video_id), headers={**UA, "Cookie": "CONSENT=YES+1"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:
            result = r.status == 200
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400 and "/watch" in (e.headers.get("Location") or ""):
            result = False
        else:
            raise RuntimeError(f"unexpected answer {e.code} for {video_id}")
    _short_cache[video_id] = result
    return result


def skip_as_short(title, link):
    """True if this YouTube item is a Short and must not be posted."""
    key = link_key(link)
    if not key.startswith("yt:"):
        return False
    vid = key[3:]
    if "/shorts/" in link or re.search(r"#shorts?\\b", title, re.I):
        return True
    try:
        return is_short(vid)
    except Exception as e:
        _short_fails[vid] = _short_fails.get(vid, 0) + 1
        if _short_fails[vid] < SHORTS_RETRIES:
            print(f"[wait] can't tell yet if {vid} is a Short ({e}); will retry")
            raise
        print(f"[warn] giving up checking {vid}; posting it")
        return False


def save_state(seen):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen[-KEEP_SEEN:], f, indent=0)
    os.replace(tmp, STATE_FILE)                  # never leaves a half-written file


def push_state():
    """On GitHub, save the memory file so a restart never re-posts old items."""
    if DRY_RUN or not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", "update seen list"], check=True)
        for attempt in range(4):
            subprocess.run(["git", "pull", "--rebase", "-q"], check=False)
            if subprocess.run(["git", "push", "-q"]).returncode == 0:
                return
            time.sleep(2 + attempt * 3)
        print("[warn] could not push the memory file after several tries")
    except Exception as e:
        print(f"[warn] could not save memory file: {e}")


def persist(seen):
    save_state(seen)
    push_state()


def check_once(seen, first_run):
    """One pass over all feeds. Posts anything new. Returns True if `seen` changed."""
    seen_set = set(seen)
    new_items = []  # (label, title, link)
    for label, addr in load_sources():
        try:
            if addr.startswith("youtube:"):
                handle = addr.split(":", 1)[1]
                if handle not in _yt_cache:
                    _yt_cache[handle] = resolve_youtube(handle)
                items = youtube_items(_yt_cache[handle])
            else:
                items = parse_feed(fetch(addr))
        except Exception as e:  # one broken website must not stop the others
            print(f"[skip] {label}: {e}")
            continue
        items = [(t, l) for t, l in items if "/shorts/" not in l]   # never post Shorts
        fresh = []
        for t, l in items:
            keys = keys_for(t, l)
            if any(k in seen_set for k in keys):
                continue
            seen_set.update(keys)      # also blocks the same story arriving from another feed this pass
            fresh.append((label, t, l))
        if fresh:
            print(f"{label}: {len(items)} items, {len(fresh)} new")
        new_items.extend(reversed(fresh))   # oldest first

    if first_run:
        print("First run: remembering current items without posting them.")
        for _, t, l in new_items:
            seen.extend(keys_for(t, l))
        return True

    changed = False
    if len(new_items) > MAX_POSTS_PER_RUN:
        for _, t, l in new_items[:-MAX_POSTS_PER_RUN]:
            seen.extend(keys_for(t, l))     # skipped extras are marked as seen
            changed = True
        new_items = new_items[-MAX_POSTS_PER_RUN:]
    if changed:
        persist(seen)

    for label, title, link in new_items:
        keys = keys_for(title, link)
        seen.extend(keys)               # remember it BEFORE sending, so a cancel mid-send can't cause a repeat
        if send(label, title, link):
            changed = True
            if not DRY_RUN:
                persist(seen)           # save to GitHub right away
                time.sleep(2)
        else:
            del seen[-len(keys):]       # sending failed: forget it so it is retried next pass
    return changed


def load_state():
    """Load the memory file; older entries (plain links) are converted to the new key format."""
    with open(STATE_FILE) as f:
        raw = json.load(f)
    out, have = [], set()
    for entry in raw:
        k = link_key(entry)
        if k not in have:
            have.add(k)
            out.append(k)
    return out


def main():
    if not DRY_RUN and (not TOKEN or not CHAT_ID):
        sys.exit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secrets.")

    first_run = not os.path.exists(STATE_FILE)
    seen = [] if first_run else load_state()

    def on_stop(signum, frame):
        # GitHub sends this when a run is cancelled or timed out: save memory before dying
        print("Stop requested - saving memory before exit.")
        if not DRY_RUN:
            persist(seen)
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    deadline = time.time() + LOOP_MINUTES * 60   # LOOP_MINUTES=0 -> single pass
    while True:
        try:
            changed = check_once(seen, first_run)
        except Exception as e:  # never die because of a temporary problem
            print(f"[error] {e}")
            changed = True      # something may have been posted before the error: save to be safe
        first_run = False
        if changed and not DRY_RUN:
            persist(seen)
        if time.time() + POLL_SECONDS >= deadline:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
