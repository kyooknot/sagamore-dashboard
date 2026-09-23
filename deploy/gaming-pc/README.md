# ES-DE → Sagamore Now Playing

Reports the retro game ES-DE just launched, with its box art, to
`POST /api/ingest/esde`. Installed on **AbrahamLincoln** (`ssh gaming-pc`, CachyOS).

## Why push

Nothing outside ES-DE knows a libretro core just started. There is no API to ask and no
process name that maps to a game — the same RetroArch binary runs every system, and PSX
and PS2 are separate AppImages in `~/Applications`. ES-DE does know, and it runs custom
event scripts, so it reports in. Same pattern as pve's patch push.

## Layout

| path | what |
|---|---|
| `~/.local/bin/sagamore-esde` | the worker (`start` / `stop`) |
| `~/ES-DE/scripts/{game-start,game-end,quit}/10-sagamore.sh` | thin hooks, run by ES-DE |
| `~/.config/sagamore-esde.env` | `INGEST_TOKEN=…`, **mode 600** |
| `~/.local/state/sagamore-esde.log` | every invocation, including raw argv |

Requires `CustomEventScripts` = `true` in `~/ES-DE/settings/es_settings.xml`.
⚠️ **ES-DE rewrites that file when it exits**, so edit it only while ES-DE is closed or
the change is silently reverted.

## Traps this already hit

🚨 **Never pass the encoded art through the environment or argv.** A single env var or
argument is capped at `MAX_ARG_STRLEN` = **128 KB** on Linux, whatever `ARG_MAX` says.
Box art base64s to ~370 KB, so `python3` never ran (`Argument list too long`, exit 126),
`curl` posted an **empty body**, and Sagamore answered 500. It would have failed for
every cover over ~96 KB — most of them — while the game name still looked fine in
testing with a small image. The worker passes the *path* and lets Python read the file.

⚠️ **Box art is keyed on the ROM FILE BASENAME, not the display name**, and lives under
`MediaDirectory`: `/mnt/frank-media/<system>/covers/<basename>.{png,jpg}`. The gamelist
`<image>` entries point at `./images/<name>-image.png` and **those files do not exist**
on this machine — ES-DE resolves art from MediaDirectory instead. Matching on the
display name finds nothing.

⚠️ **ES-DE waits for these scripts**, so the worker detaches all slow work and always
exits 0. A dashboard row is never worth a game that will not start.

⚠️ The gaming PC's login shell is **fish**. Drive it with `ssh gaming-pc 'bash -s' <<'EOF'`;
inline quoting breaks in ways that look like script bugs.

## Staleness

`game-end` and `quit` clear the row. A crash or power cut sends neither, so Sagamore
bounds the pushed state with `ESDE_MAX_AGE` (6h) — see `now_playing_panel`.
