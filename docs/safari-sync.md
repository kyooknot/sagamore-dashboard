# Safari → Sagamore (one-time migration only)

> **⚠️ This is not the ongoing mechanism.** Reading Safari's bookmarks depends on
> **iCloud** to carry them from the phone to the Mac first, which puts a third
> party in the path of data that never needs to leave the LAN. It was retired
> for day-to-day use on 2026-08-21.
>
> **The live design is [bookmarks.md](bookmarks.md)** — a bookmarklet and an iOS
> Shortcut posting straight to Sagamore. Use this document only to bring an
> existing Safari library across once.

## The short answer

**There is no app that syncs iOS Safari bookmarks with anything.** Apple exposes
no API for them — no third-party app on iOS can read or write Safari's
bookmarks. Anything in the App Store claiming to "sync your Safari bookmarks" is
syncing its own separate store and asking you to re-enter everything.

You don't need one, because iCloud already does the hard part:

```
  iPhone Safari ──iCloud──▶ Mac Safari ──safari-sync.py──▶ linkding ──▶ Sagamore
                                                              ▲
  iPhone Safari ──── iOS Shortcut, one tap ────────────────────┘
```

Bookmarks you save on the phone appear in Safari on the Mac, and macOS Safari
keeps them in a plist we *can* read. So the Mac is the bridge for bulk sync, and
an iOS Shortcut covers instant capture from the phone.

---

## 1. Bulk: Safari → linkding (`tools/safari-sync.py`)

Reads `~/Library/Safari/Bookmarks.plist` and upserts into linkding. Safari
folders become tags (`Home Network` → `home-network`), plus a `safari` tag on
everything so you can tell where it came from.

```bash
./tools/safari-sync.py --dry-run     # see what would change
./tools/safari-sync.py               # sync
./tools/safari-sync.py --reading-list  # include Reading List (transient; off by default)
```

First run on 2026-08-21 brought over 35 bookmarks across Home Network, Smart
Home, Gaming and Entertainment.

**Notes from building it:**

- **One-way, deliberately.** Writing back into Safari would mean editing a plist
  Safari owns, caches in memory and rewrites on quit. Racing it loses bookmarks.
  Capture in the other direction is the Shortcut below.
- **Over-long URLs are skipped.** Half-finished OAuth flows get bookmarked by
  accident and can run to thousands of characters; linkding 400s on them and
  they're worthless as bookmarks. Anything over 2,000 chars is dropped with a
  count, not a stack trace.
- **Reading List is excluded by default** — it's a queue, not a library.
- **Full Disk Access** may be required for `~/Library/Safari`. If the script says
  so, grant it to your terminal in System Settings → Privacy & Security.
- It's **idempotent** — linkding's `POST /api/bookmarks/` upserts on URL, so
  re-running never duplicates.

### Scheduling — don't

The launchd job in `deploy/mac/` is **not installed and should stay that way**:
running it on a timer is exactly the iCloud dependency this design removed. It
is kept only so a one-off catch-up import is easy to run by hand.

Historical note on why it wouldn't have worked anyway:

`deploy/mac/com.example.safari-sync.plist` runs it every 6 hours — but it is
**deliberately not installed**, because it cannot work yet:

```
/usr/bin/python3: can't open file '.../safari-sync.py': [Errno 1] Operation not permitted
```

macOS **TCC** blocks it twice over: launchd's Python has neither access to
`~/Documents` nor **Full Disk Access**, and reading `~/Library/Safari/` requires
the latter. Running it by hand works because an interactive terminal already
holds those grants.

A job that fails every six hours into a log nobody reads is precisely the
silently-broken automation this project exists to prevent, so it was removed
rather than left in place.

**To enable it**, grant Full Disk Access to the interpreter launchd will use
(System Settings → Privacy & Security → Full Disk Access → add
`/usr/bin/python3`), then:

```bash
cp deploy/mac/com.example.safari-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.example.safari-sync.plist
tail -f /tmp/safari-sync.log        # confirm it actually ran
```

Note that granting FDA to `python3` is broad — it covers *every* Python script
run on this Mac. If that's unwelcome, the alternatives are to run the sync by
hand when you've bookmarked a batch on the phone, or to rely on the iOS Shortcut
below, which needs no Mac involvement at all.

---

## 2. Instant: iPhone → linkding (iOS Shortcut)

For "I'm reading this on my phone and want it filed now". One tap from Safari's
share sheet, no app to install.

**Build it once** (Shortcuts app → **+** → Add Action):

1. At the top, tap the shortcut settings and turn on **Show in Share Sheet**,
   with **Accepted Types** set to *URLs* only.
2. Add **Get Contents of URL**, and set:
   - **URL**: `https://links.example.com/api/bookmarks/`
   - **Method**: `POST`
   - **Headers**:
     - `Authorization` → `Token <your linkding API token>`
     - `Content-Type` → `application/json`
   - **Request Body**: `JSON`
     - `url` (Text) → the **Shortcut Input** variable
     - `title` (Text) → leave empty; linkding fetches the page title itself
     - `tag_names` (Array) → `phone`
3. Name it something like **Save to Sagamore**.

Then: Share → *Save to Sagamore*, and it appears on `sagamore.example.com` within
15 minutes (Sagamore mirrors linkding every 15 min; refresh linkding itself to
see it immediately).

The token lives in the vault at `<your-vault>/LINKDING_TOKEN`. Prefer a
**separate** token for the phone so it can be revoked on its own — create one in
linkding under Settings → Integrations → REST API.

> `links.example.com` resolves only on the LAN and over Tailscale, so the
> Shortcut works from anywhere the tailnet reaches. With Tailscale off and away
> from home, it will fail — that's the perimeter working as designed.

---

## 3. What about going the other way?

Getting linkding **into** Safari isn't worth doing:

- Writing `Bookmarks.plist` behind Safari's back is unsupported and risks the
  file.
- linkding is already a better reader than Safari's bookmark list — full-text
  search, tags, archiving.
- Add `links.example.com` to your iPhone home screen; linkding ships a PWA, so
  it behaves like an app without being one.

If you ever do want a one-off export, linkding has **Settings → Export** which
emits the same Netscape HTML format Safari imports.
