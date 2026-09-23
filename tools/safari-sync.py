#!/usr/bin/env python3
"""Sync Safari bookmarks (including the ones you make on iOS) into linkding.

**Why this exists rather than an app.** Apple exposes no API for Safari
bookmarks on iOS — no third-party app can read or write them, and anything
claiming to "sync Safari bookmarks" is really syncing its own separate store.

But iCloud already does the hard part: bookmarks you save in Safari on the
iPhone appear in Safari on the Mac. And macOS Safari keeps them in a readable
property list. So the Mac is the bridge — this reads that plist and upserts into
linkding, which Sagamore then mirrors.

**One-way, by design.** Safari → linkding only. Writing back would mean editing
`Bookmarks.plist`, a file Safari owns, caches in memory and rewrites on quit;
racing it is a good way to lose bookmarks. Capture from the phone in the other
direction is handled by an iOS Shortcut hitting linkding's API — see
docs/safari-sync.md.

Usage:
    ./safari-sync.py --dry-run          # show what would change
    ./safari-sync.py                    # sync
    ./safari-sync.py --reading-list     # include Safari's Reading List too

Needs LINKDING_URL and LINKDING_TOKEN in the environment, or ~/.config/sagamore/env.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SAFARI_PLIST = Path.home() / "Library" / "Safari" / "Bookmarks.plist"
READING_LIST = "com.apple.ReadingList"
# Safari's own containers, which are structure rather than user folders.
STRUCTURAL = {"BookmarksBar", "BookmarksMenu", "History", ""}

# Practical ceiling; linkding 400s well before this and such URLs are junk.
MAX_URL = 2000


def load_env() -> None:
    """Environment wins; otherwise read the deploy-time rendered file."""
    path = Path.home() / ".config" / "sagamore" / "env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return s or "unsorted"


def read_safari(include_reading_list: bool) -> list[dict]:
    if not SAFARI_PLIST.exists():
        raise SystemExit(f"Safari bookmarks not found at {SAFARI_PLIST}")
    try:
        with SAFARI_PLIST.open("rb") as f:
            root = plistlib.load(f)
    except PermissionError:
        raise SystemExit(
            f"Cannot read {SAFARI_PLIST}.\n"
            "macOS requires Full Disk Access for ~/Library/Safari — grant it to "
            "your terminal in System Settings > Privacy & Security > Full Disk Access."
        )

    out: list[dict] = []
    seen: set[str] = set()
    skipped: list[str] = []

    def walk(node: dict, folder: str) -> None:
        for child in node.get("Children", []) or []:
            kind = child.get("WebBookmarkType")
            if kind == "WebBookmarkTypeList":
                title = child.get("Title", "") or ""
                # Reading List is a system container, not a folder the user made.
                if title == READING_LIST and not include_reading_list:
                    continue
                nxt = folder if title in STRUCTURAL else (title or folder)
                if title == READING_LIST:
                    nxt = "reading-list"
                walk(child, nxt)
            elif kind == "WebBookmarkTypeLeaf":
                url = child.get("URLString", "") or ""
                if not url.startswith(("http://", "https://")) or url in seen:
                    continue
                # Half-finished OAuth flows get bookmarked by accident and can
                # run to thousands of characters; linkding rejects them with a
                # 400 and they are worthless as bookmarks anyway.
                if len(url) > MAX_URL:
                    skipped.append(url)
                    continue
                seen.add(url)
                title = (child.get("URIDictionary") or {}).get("title") or url
                out.append({"url": url, "title": title.strip(), "folder": folder})

    walk(root, "safari")
    if skipped:
        print(f"  skipped {len(skipped)} over-long URL(s) (stale OAuth/login flows)")
    return out


class Linkding:
    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Token {self.token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def existing_urls(self) -> set[str]:
        urls: set[str] = set()
        path = "/api/bookmarks/?limit=500"
        while path:
            body = self._req("GET", path)
            for b in body.get("results", []):
                urls.add(b.get("url", ""))
            nxt = body.get("next")
            path = nxt[len(self.base):] if nxt and nxt.startswith(self.base) else None
        return urls

    def upsert(self, url: str, title: str, tag: str) -> None:
        # linkding's POST /api/bookmarks/ upserts on URL, so this is safe to
        # re-run; it will not create duplicates.
        self._req("POST", "/api/bookmarks/",
                  {"url": url, "title": title, "tag_names": [tag, "safari"]})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    ap.add_argument("--reading-list", action="store_true",
                    help="include Safari's Reading List (transient by nature)")
    args = ap.parse_args()

    load_env()
    base, token = os.environ.get("LINKDING_URL"), os.environ.get("LINKDING_TOKEN")
    if not (base and token):
        print("LINKDING_URL / LINKDING_TOKEN not set", file=sys.stderr)
        return 2

    marks = read_safari(args.reading_list)
    print(f"Safari: {len(marks)} bookmarks in {len({m['folder'] for m in marks})} folders")

    ld = Linkding(base, token)
    try:
        have = ld.existing_urls()
    except urllib.error.URLError as exc:
        print(f"linkding unreachable: {exc}", file=sys.stderr)
        return 1

    new = [m for m in marks if m["url"] not in have]
    print(f"linkding: {len(have)} existing · {len(new)} new from Safari")

    if args.dry_run:
        for m in new[:40]:
            print(f"  + [{slug(m['folder'])}] {m['title'][:64]}")
        if len(new) > 40:
            print(f"  … and {len(new) - 40} more")
        return 0

    ok = err = 0
    for m in marks:
        try:
            ld.upsert(m["url"], m["title"], slug(m["folder"]))
            ok += 1
        except Exception as exc:  # noqa: BLE001
            err += 1
            if err <= 3:
                print(f"  ! {m['url']}: {str(exc)[:70]}", file=sys.stderr)
    print(f"synced {ok} ok, {err} failed")
    return 1 if err and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
