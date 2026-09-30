# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A minimal package-based Discord bot (discord.py). Each file in `packages/` is a
[discord.py extension](https://discordpy.readthedocs.io/en/stable/ext/commands/extensions.html):
a module with an `async def setup(bot)` that adds a Cog. `bot.py` discovers and
loads every module in `packages/` on startup via `pkgutil.iter_modules`, so
adding a new feature means dropping a new file in `packages/` — no registration
step elsewhere. Packages are isolated: one failing to load doesn't take down
the others.

Currently there is one package, `packages/music.py`, implementing YouTube
playback via `yt-dlp` + `discord.py`'s voice/FFmpeg support, all as Discord
slash commands (`app_commands`).

**Every module in `packages/` must define `async def setup(bot)`** — the loader
imports all of them indiscriminately, so a helper module dropped in there fails
with `NoEntryPointError`. Shared non-Discord code goes at the top level instead
(see `player.py`).

## Commands

Run the bot (requires `.env` with `DISCORD_TOKEN`, optionally `GUILD_ID`):
```sh
docker compose up -d --build
```

Run the self-check test for the queue logic:
```sh
python test_music.py
```

There is no test framework — `test_music.py` is a plain script of asserts
(run directly, not via pytest). It needs no dependencies installed, because it
covers `player.py`, which deliberately imports nothing. Keep new pure logic
importable without `discord`/`yt_dlp` so it stays testable outside Docker, and
add a similar `assert`-based script rather than introducing a test framework.

## Architecture

**Command sync**: `bot.py`'s `setup_hook` syncs the app command tree to a
single guild (instant, for dev) when `GUILD_ID` is set, otherwise syncs
globally (slow to propagate, for production).

**Per-guild state**: `Music` cog keeps one `Player` per guild ID in
`self.players`, created lazily via `self.player(guild_id)`. There is no
cross-guild state.

**Queue model** (`Player` in `player.py`): a single list (`queue`)
plus an `index` pointing at the current track — played tracks are never
removed, so `/back` and `/replay` just move `index` backward. `jump` is a
one-shot override consumed by `step()`, used to implement `/next`, `/back`,
and `/replay` by stopping the current FFmpeg playback and letting the
`after=` callback's `advance()` pick up the jump. Mutating operations
(`move`, `remove`, `clear`) only ever touch the *upcoming* part of the queue
(index > current) to avoid corrupting what's currently playing or already
played.

**Failure handling**: `play_index` loops rather than recursing, skipping tracks
`yt-dlp` can't resolve (unavailable/private/geo-blocked). This matters because
it runs from the FFmpeg `after` callback via `run_coroutine_threadsafe` — an
exception there lands in a Future nobody awaits and would silently end playback
for the rest of the queue.

**Two playback invariants** — both exist because resolving a stream URL awaits,
so playback starts can interleave:

1. `play_index` holds `Player.lock` for the whole resolve-and-play section and
   bails if `vc.is_playing()`. Without both, `/play` and a track ending at the
   same moment each reach `vc.play()` → `ClientException: Already playing
   audio.` plus a dropped track.
2. The queue UI addresses tracks by `track["id"]` (a uuid set in
   `tracks_from`), never by position — `QueueView.selected` is an id, and
   `Player.find()` resolves it at click time. Positions shift under a view as
   the queue is edited or advances, so a stored index silently targets the
   wrong track.

**Four yt-dlp instances, each for one job.** A playlist can hold thousands of
entries (1619 in one measured case, 7.2s to enumerate), so `/play` never
enumerates one unless asked:

| | options | why |
|---|---|---|
| `YTDL_PROBE` | `playlist_items: "1"` | classify the query + confirm non-empty. **0.8s vs 7.1s** — fast enough to answer in-band |
| `YTDL_SOLO` | `noplaylist: True` | the one song the link points at (see below) |
| `YTDL_FULL` | — | the whole playlist, enumerated *after* playback starts |
| `YTDL_STREAM` | — | per-track stream URL at play time |

**`is_playlist()` keys on the extractor, not `_type`.** A plain text search also
reports `_type == "playlist"` (verified), so testing `_type` alone would pop the
prompt on every ordinary song search. `youtube:search` vs `youtube:tab` is the
real discriminator.

**`noplaylist` is load-bearing.** For a `watch?v=X&list=Y` URL — what you get
copying a song from *inside* a playlist — yt-dlp resolves to the playlist and
hands back **item 1, not video X** (verified). `YTDL_SOLO` is how both buttons
find the track the link actually points at, and it still returns item 1 for a
bare `playlist?list=` URL, so `PlaylistPrompt._start_track` covers both shapes
with one call.

**Playlists queue from the linked song, not from entry 1.** `load_rest` locates
the start track by `vid` in the full enumeration and appends only what follows
it, so pasting song 40 of a playlist queues 40→N the way YouTube behaves. It
does *not* wrap around to 1–39. If the id isn't found it falls back to entry 1
onward.

**`tracks_from` prefers `webpage_url` over `url`, and the order matters.** On a
fully extracted video `url` is the direct googlevideo stream carrying an
`expire=` timestamp, which would rot while the track waits in the queue; flat
playlist entries are the reverse — no `webpage_url`, watch link in `url`. Each
track also keeps `vid` (yt-dlp's video id) so `load_rest` can find its position.

**What `/play` does.** Playlists prompt via `PlaylistPrompt`; everything else
goes straight through. `Music.enqueue(..., ahead=)` is the single place the
add/play rules live — `ahead=True` means one explicitly requested song:

| | nothing playing | already playing/paused |
|---|---|---|
| single song (`ahead=True`) | **inserted at `index + 1` and played now** | appended, nothing interrupted |
| playlist (`ahead=False`) | appended; playback starts at `index + 1` | appended, nothing interrupted |

`ahead` must not be inferred from `len(tracks) == 1`: the "add entire playlist"
path calls `enqueue` with just entry 1 so music can start, and inferring would
wrongly insert it ahead of tracks already pending.

`load_rest` then appends the remaining entries while that first track plays. It
re-checks `self.players.get(guild.id) is p` afterwards so a `/exit` mid-load
drops the late arrivals, and restarts playback if the first track finished
before the rest landed (measured: 2.5s to music vs 8.7s on a 1620-track list).

Two invariants make this work, and both are easy to break:

- **A single song is `insert`ed, not appended.** Appending and then playing it
  would walk `index` *past* anything still pending, and both view layers filter
  relative to the index — `queue_embed` uses `p.index <= i`, `QueueView` uses
  `i > p.index` — so those tracks would vanish from `/queue` and `/next`
  entirely. Inserting keeps them after the index and reachable.
- **Playback always starts at `index + 1`, never `len(queue)`.** `index` points
  at the current/last-played track, so `index + 1` is the oldest *unplayed* one:
  the song just inserted, or whatever was already pending for a playlist. When
  the queue has played through, `index + 1` already equals the first appended
  track, so one expression covers every case without a branch.

**Stream URLs are resolved per-track, late.** The queue stores watch-page links;
`YTDL_STREAM` turns one into a playable URL immediately before `vc.play()`.
Resolving up front would not survive a long queue — googlevideo URLs carry an
`expire=` timestamp. Every `yt_dlp` call goes through `asyncio.to_thread`, since
the library is blocking.

**Now-playing panel** (`NowPlaying` + `Music.panels`, one per guild). Differs
from `QueueView` in two ways that matter:

- **`timeout=None`.** It must stay usable as long as the bot is in voice, so it
  cannot expire on its own. `close_panel` is what retires it, which is also what
  keeps views from leaking.
- **Buttons are built once; `sync(guild)` re-points them** (toggle icon/label,
  per-edge disabling) before each redraw. `QueueView` rebuilds instead because
  its option list changes; here the five controls are fixed, so mutating in place
  avoids the stop-and-replace dance.

`ensure_panel(guild, channel)` posts the panel if the guild hasn't got one and
redraws it otherwise, so the panel appears by itself as soon as the bot joins
voice. It is called from all three `/play` paths (plain `/play`, and both
`PlaylistPrompt` buttons) — in the "add entire playlist" case *before*
`load_rest`, or the panel would be held back by the whole enumeration. It
swallows send failures: no Send Messages permission should cost the panel, not
the music. `/nowplaying` stays useful for re-posting the panel once chat has
scrolled past it, and retires the previous one so only ever one is live.

`refresh_panel` is called wherever displayed state changes: a new track in
`play_index`, the queue running dry in `advance`, `/pause`, `/resume`, `/clear`,
and `QueueView.refresh` (which can change "Up next"). **It must never raise** —
it runs from the FFmpeg `after` callback path, where an exception lands in an
un-awaited Future and silently kills the queue, so it swallows and logs instead.

`on_voice_state_update` closes the panel when the bot leaves voice. Every exit
routes through it — `/stop`, `/exit`, a moderator disconnect, a deleted channel —
so no per-command cleanup is needed.

Panel buttons and slash commands share `Music.jump_to(guild, i)`; `_jump` is just
the interaction-aware wrapper that replies first (3s window) and delegates.

**Queue UI**: `/queue` renders a `discord.Embed` plus a `QueueView`
(`discord.ui.View`) with a track select + move/remove/clear buttons. Every
button/select callback re-renders through `refresh()`, which builds a *fresh*
`QueueView` and calls `interaction.response.edit_message`. Two things must carry
across that rebuild, or the view breaks subtly:

- `selected` (a track id) and `msg` are copied to the new view.
- `refresh()` calls `self.stop()` on the outgoing view. Without it the dead
  view's 180s timeout still fires and `on_timeout` edits the message with its
  own stale, disabled controls, wiping the live ones.

## Adding a new package

Create `packages/<name>.py` with a Cog and an `async def setup(bot):
await bot.add_cog(YourCog(bot))`. It will be picked up automatically; no
changes to `bot.py` are needed.

## TODO

**Commit tests for `packages/music.py`.** Coverage is currently inverted: the
stable file is tested, the volatile one is not.

```
player.py          7 methods,  20 assertions   <- tested
packages/music.py  ~40 methods,  0 assertions  <- 521 lines, untested
```

Everything reworked recently lives in the untested file — `enqueue`'s `ahead=`
rules, `load_rest`'s slice-from-linked-song, `tracks_from`'s watch-URL
preference, `is_playlist`, all of `PlaylistPrompt`, `play_index`'s lock and
dead-track skip, `_jump`'s reply-first ordering, `QueueView._resolve`.

Two things to do:

1. **Add `test_play.py`** covering at minimum: `/play` for search / single video
   / playlist; both prompt buttons idle and mid-song; a playlist yielding to
   already-pending tracks; slicing when the link names a song partway through;
   the linked id being absent or last; wrong-user click refused; prompt timeout
   queueing nothing; `load_rest` failing partway; the first track ending
   mid-load; `/exit` during a load dropping late arrivals; `tracks_from`
   rejecting the expiring googlevideo URL.
2. **Rename `test_music.py` → `test_player.py`** (it imports `player`, not
   `music` — the name currently implies coverage that does not exist) and update
   the reference in README.md and in the Commands section above.

Tests must run with no dependencies installed, like `test_music.py` does.
`packages/music.py` imports `discord` and `yt_dlp` at module level, so
`test_play.py` needs to stub those in `sys.modules` before importing it —
`discord.ui.View` and `commands.Cog` have to be real classes (they get
subclassed); the rest can be `MagicMock`/`SimpleNamespace`. Fakes worth building
once: a voice client that raises if `play()` is called while already playing
(that assertion is what catches accidental interruptions), and an interaction
recording `followup`/`edit_original_response` calls.

Each of these listed cases has caught a real bug during development, so they are
regression guards, not coverage theater.
