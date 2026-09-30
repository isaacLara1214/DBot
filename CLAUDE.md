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
| `YTDL_PROBE` | `playlist_items: "1"` | classify the query + get entry 1. **0.8s vs 7.2s** — fast enough to answer in-band |
| `YTDL_SOLO` | `noplaylist: True` | the one song the user linked (see below) |
| `YTDL_REST` | `playlist_items: "2:"` | the remainder, fetched *after* playback starts |
| `YTDL_STREAM` | — | per-track stream URL at play time |

**`is_playlist()` keys on the extractor, not `_type`.** A plain text search also
reports `_type == "playlist"` (verified), so testing `_type` alone would pop the
prompt on every ordinary song search. `youtube:search` vs `youtube:tab` is the
real discriminator.

**`noplaylist` is load-bearing for "just one song".** For a
`watch?v=X&list=Y` URL — what you get copying a song from *inside* a playlist —
yt-dlp resolves to the playlist and hands back **item 1, not video X**
(verified). `YTDL_SOLO` avoids playing a different song than the one pasted, and
still returns item 1 for a bare `playlist?list=` URL, so one path covers both.

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

`load_rest` then appends entries 2…N while that first track plays. It re-checks
`self.players.get(guild.id) is p` afterwards so a `/exit` mid-load drops the
late arrivals, and restarts playback if the first track finished before the rest
landed.

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

**Playback flow**: `/play` extracts track metadata with a flat, non-streaming
`yt_dlp.YoutubeDL` (`YTDL_FLAT`, fast — handles search terms, single videos,
and playlists) and appends to the queue. Actual playback resolves the
streamable URL lazily per-track with a second `YoutubeDL` instance
(`YTDL_STREAM`) right before `vc.play()`, since flat extraction doesn't give
a directly playable stream URL. Both extractions run in an executor
(`run_in_executor`) since `yt_dlp` is blocking.

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
