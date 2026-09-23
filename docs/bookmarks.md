# Bookmarks — de-clouded, one store

## The design

**Nextcloud Bookmarks is the single store, and nothing leaves the house.**

```
   desktop browser ─────Floccus extension──▶ ┌──────────────┐
   any browser ─────────bookmarklet────────▶ │  Nextcloud   │──▶ Sagamore panel
   iPhone ──────────────Sagamore / NC app──▶ │  Bookmarks   │
                                             └──────────────┘
                          (LAN / tailnet only, no third party)
```

> **This store moved twice, and the reasoning is the useful part.** It began on
> linkding, moved to Linkwarden because Floccus can use it as a sync backend,
> and ended on Nextcloud Bookmarks — because Nextcloud was already running for
> other reasons, so the final move *removed* a service rather than adding one.
>
> Both migrations were verified by count **and** by URL-set comparison, not just
> "it looks right". If you do the same, take a backup of the old service before
> decommissioning it: the cost is near zero and it is the only thing that makes
> the migration reversible.

It does two jobs that used to need two systems:

1. **Native browser bookmarks**, synced by [Floccus](https://floccus.org) — so the
   bookmark bar and address-bar autocomplete keep working, on every desktop.
2. **The searchable library** — collections, tags, full-text search, archiving —
   which Sagamore mirrors into its Bookmarks panel.

### Why Nextcloud Bookmarks and not linkding

linkding served the library well but cannot back Floccus, so browser-native
bookmarks would have stayed on iCloud or nowhere. Floccus supports Nextcloud,
WebDAV, Git, **Linkwarden** and KaraKeep — picking Linkwarden collapses two
services into one. Migrated 2026-08-21, all 56 bookmarks into 10 collections.

### The constraint worth stating plainly

**Safari cannot participate in any of this.** Floccus [does not support
Safari](https://floccus.org/faq/), no extension may write Safari's bookmarks, and
Safari's own sync *is* iCloud. Since Safari also doesn't exist on Linux, it is a
dead end for a de-clouded, cross-platform setup — which is why the browser-native
half runs in Firefox/Brave/Orion instead.

---

## Floccus — native bookmarks, self-hosted

Install the Floccus extension, then point it at Nextcloud:

| | |
|---|---|
| Server | `https://nextcloud.example.com` |
| Auth | username + an **app password** (Settings → Security → *Create new app password*). Make a *separate* one per device so any of them can be revoked alone — and never the account password, which also won't work once SSO is in front |
| Sync target | leave the server path empty for the top-level folder, or name one to keep synced browser bookmarks apart from the curated library |

Runs on Chromium, Chrome, Firefox, Edge, Opera, Brave, Vivaldi and **Orion**.

### On iOS

**Tested 2026-09-01 on the device: it does not work.** Floccus installs and runs
as an Orion extension on iOS, authenticates fine, and shows "All good" — but it
never syncs, and pressing *sync down* does nothing at all.

The server logs settle it. Across the whole session the Orion extension made
**zero** calls to `/apps/bookmarks/public/rest/v2/` — it only ever POSTed to
`/login/v2`. It authenticates, then stops before touching the bookmarks API. The
cause is local, not server-side: WebKit on iOS does not expose the `bookmarks`
WebExtension API, so Floccus has no local tree to diff and aborts without
surfacing an error.

**No sync backend fixes this** — it is not a Linkwarden or Nextcloud problem, and
swapping stores changes nothing. What *did* work from the same phone, in the same
session, was the [Floccus iOS companion
app](https://apps.apple.com/us/app/floccus/id1626998357): it locked, fetched the
folder hash, pulled the full tree and unlocked, all cleanly. So on iOS the
options are that companion app, the Nextcloud app, or the Sagamore panel —
all of which *read* bookmarks rather than putting them in the browser's own list.

The trap for a future session: the extension **appearing to install and run** in
Orion looks like proof it works. It isn't. Check for actual REST calls in the
Nextcloud access log before believing it.

---

## Mac / any desktop browser — the bookmarklet

On `sagamore.example.com`, under **Bookmarks → Add or import**, drag **▸ Save to
Sagamore** onto your bookmarks bar. Then one click files the page you're on.

It works in Safari with no extension — useful while you're still on macOS, since
Floccus can't touch Safari at all. On any browser Floccus *does* support, the
extension is the better path; the bookmarklet is for filing one-offs into the
Inbox from anywhere.

```js
javascript:(function(){
  var u=encodeURIComponent(location.href), t=encodeURIComponent(document.title);
  window.open('https://sagamore.example.com/bookmarks/capture?url='+u+'&title='+t+'&tags=inbox',
              'sagamore','width=380,height=190');
})();
```

The popup confirms and closes itself after a second.

## iPhone — the Shortcut

Shortcuts app → **+** → turn on **Show in Share Sheet** (Accepted Types: *URLs*)
→ add **Get Contents of URL**:

- **URL** `https://sagamore.example.com/bookmarks/capture`
- **Method** `POST`
- **Request Body** `JSON`
  - `url` → the **Shortcut Input** variable
  - `tags` → `inbox phone`

Name it *Save to Sagamore*. That's the whole thing — **no token**, because
Sagamore holds it.

> Works anywhere the tailnet reaches. Off Tailscale and away from home it fails,
> which is the perimeter behaving correctly.

## Reading on the phone

Add `links.example.com` to the home screen — Linkwarden is a PWA, so it
behaves like an app without being one. Or just read the Bookmarks panel on
`sagamore.example.com`.

---

## The endpoint

`GET|POST /bookmarks/capture` — `url` (required), `title`, `tags`.

Saves to Linkwarden's **Inbox** collection **and** keeps a local copy, so a Linkwarden outage never loses a
capture. GET is accepted deliberately: a bookmarklet can't POST cross-origin
without CORS, but it can open a URL — the same trick every hosted read-later
service has used since Delicious. It's a mutation behind a GET, which is only
acceptable because the endpoint is LAN/tailnet-only and purely additive.

There is **no authentication**, matching the rest of Sagamore: the
perimeter is the tailnet. Anyone already inside it can file a bookmark, which is
the correct blast radius for this feature.

---

## Turning off iCloud bookmark sync

Optional, and entirely your call — the design above doesn't depend on it either
way. If you want Safari's bookmarks to stop leaving your devices:

**iPhone** → Settings → *your name* → iCloud → Saved to iCloud → See All →
**Safari** → off.
**Mac** → System Settings → *your name* → iCloud → **Safari** → off.

Turning it off keeps the bookmarks already on each device; it just stops them
syncing. Do the one-time import **first** if you haven't, or the Mac copy may
lose anything that only ever existed on the phone.
