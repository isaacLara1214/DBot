import asyncio
import uuid

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from player import Player

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
YTDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "extract_flat": "in_playlist",
}
# Classifies the query and grabs its first entry. Capped to one entry because a
# few-hundred-song playlist takes seconds to enumerate in full (~0.3s vs ~1.9s
# measured), and we need to answer the user before that finishes.
YTDL_PROBE = yt_dlp.YoutubeDL({**YTDL_OPTS, "playlist_items": "1"})
# The one song the user actually linked. `noplaylist` ignores a &list= wrapper,
# so a link copied from inside a playlist resolves to that video rather than
# the playlist's first item; a bare playlist URL still yields its first item.
YTDL_SOLO = yt_dlp.YoutubeDL({**YTDL_OPTS, "noplaylist": True, "playlist_items": "1"})
# The whole playlist, enumerated once playback is already going. Unsliced because
# the starting point is dynamic: a link naming a song starts the queue there.
YTDL_FULL = yt_dlp.YoutubeDL(YTDL_OPTS)
YTDL_STREAM = yt_dlp.YoutubeDL(
    {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
)


def tracks_from(info: dict) -> list[dict]:
    """Queue entries from a yt-dlp result, skipping anything unplayable."""
    tracks = []
    for e in info.get("entries") or [info]:
        if not e:
            continue
        # webpage_url first: on a fully extracted video `url` is the direct
        # googlevideo stream, which carries an expiry and would rot while the
        # track sits in the queue. Flat playlist entries have no webpage_url and
        # put the watch link in `url`, so the fallback covers them.
        url = e.get("webpage_url") or e.get("url")
        if not url and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url:
            continue  # nothing playable; skip rather than queue a bad entry
        # Flat entries carry a `thumbnails` list; a full extraction has a single
        # `thumbnail` string. Both shapes reach here.
        thumb = e.get("thumbnail")
        if not thumb:
            thumbs = e.get("thumbnails") or []
            thumb = thumbs[-1].get("url") if thumbs else None
        tracks.append(
            {
                # Stable handle for the queue UI: positions shift, ids don't.
                "id": uuid.uuid4().hex,
                # yt-dlp's own video id, used to locate this track in a playlist.
                "vid": e.get("id"),
                "title": e.get("title") or "Unknown",
                "url": url,
                # Display-only, for the now-playing panel. Free: both the flat
                # and the full extraction already return them.
                "duration": e.get("duration"),
                "uploader": e.get("uploader") or e.get("channel"),
                "thumb": thumb,
            }
        )
    return tracks


def fmt_duration(seconds) -> str:
    """m:ss, or h:mm:ss past the hour. Live streams report no duration."""
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def is_playlist(info: dict) -> bool:
    """Whether a probe result is a real playlist worth prompting about.

    A plain text search also reports `_type == "playlist"`, so that alone would
    fire the prompt on every ordinary song search. The extractor is what tells
    them apart: `youtube:search` for searches, `youtube:tab` for playlists.
    """
    return info.get("_type") == "playlist" and "search" not in (
        info.get("extractor") or ""
    )


class PlaylistPrompt(discord.ui.View):
    """Asks whether a playlist link means the whole playlist or a single song.

    Only the first entry is known at this point (see YTDL_PROBE), so the whole
    playlist is never enumerated unless the user actually asks for it.
    """

    def __init__(self, cog: "Music", user_id: int, query: str, title: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id  # only the requester may answer
        self.query = query
        self.title = title
        self.msg: discord.Message | None = None  # set once sent, for on_timeout
        for label, style, cb in (
            ("Add entire playlist", discord.ButtonStyle.primary, self.on_all),
            ("Just one song", discord.ButtonStyle.secondary, self.on_one),
        ):
            btn = discord.ui.Button(label=label, style=style)
            btn.callback = cb
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(
            "Only whoever ran /play can answer this.", ephemeral=True
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.msg:
            try:
                await self.msg.edit(
                    content=f"**{self.title}** — timed out, nothing added.", view=self
                )
            except discord.HTTPException:
                pass  # message gone; nothing to grey out

    async def _claim(self, interaction: discord.Interaction, note: str):
        """Answer the click and drop the buttons. Must happen before any
        extraction: Discord discards an interaction unanswered after 3s."""
        self.stop()
        await interaction.response.edit_message(content=note, view=None)

    async def _say(self, interaction: discord.Interaction, content: str):
        try:
            await interaction.edit_original_response(content=content)
        except discord.HTTPException:
            pass  # deleted, or the token expired on a very long load

    async def _start_track(self) -> list[dict]:
        """The single track the link points at — the video it names, or the
        playlist's first entry when it names none. `noplaylist` yields exactly
        that in both cases, so both buttons start from the same place.
        """
        try:
            info = await asyncio.to_thread(
                YTDL_SOLO.extract_info, self.query, download=False
            )
            return tracks_from(info)[:1]
        except Exception as err:
            print(f"Extraction failed for {self.query!r}: {err}")
            return []

    async def on_all(self, interaction: discord.Interaction):
        await self._claim(interaction, f"Loading **{self.title}**…")
        # Queue that one track first so playback starts now; the remainder of the
        # playlist is enumerated afterwards, while it plays.
        start = await self._start_track()
        if not start:
            return await self._say(interaction, "Couldn't load that playlist.")
        await self.cog.enqueue(interaction.guild, start, ahead=False)
        # Before load_rest, not after: a long playlist would hold the panel back
        # by however many seconds the enumeration takes.
        await self.cog.ensure_panel(interaction.guild, interaction.channel)
        try:
            rest = await self.cog.load_rest(
                interaction.guild, self.query, start[0].get("vid")
            )
        except Exception as err:
            print(f"Playlist load failed for {self.query!r}: {err}")
            return await self._say(
                interaction,
                f"Queued **1** track from **{self.title}** — couldn't load the rest.",
            )
        await self._say(
            interaction, f"Queued **{1 + rest}** tracks from **{self.title}**."
        )

    async def on_one(self, interaction: discord.Interaction):
        await self._claim(interaction, "Loading…")
        tracks = await self._start_track()
        if not tracks:
            return await self._say(interaction, "Couldn't load that song.")
        started = await self.cog.enqueue(interaction.guild, tracks, ahead=True)
        verb = "Playing" if started else "Queued"
        await self._say(interaction, f"{verb} **{tracks[0]['title']}**")
        await self.cog.ensure_panel(interaction.guild, interaction.channel)


class NowPlaying(discord.ui.View):
    """Playback controls for the current track. One per guild, held in
    `Music.panels`.

    `timeout=None` on purpose: the panel must stay usable for as long as the bot
    is in voice, however long that is. `Music.close_panel` retires it instead,
    which is what stops this from leaking views. Buttons are built once and
    `sync()` re-points them at current state, so a redraw never swaps the view.
    """

    def __init__(self, cog: "Music", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.msg: discord.Message | None = None
        self.prev = discord.ui.Button(emoji="⏮", label="Prev")
        self.toggle = discord.ui.Button(emoji="⏸", label="Pause")
        self.nxt = discord.ui.Button(emoji="⏭", label="Next")
        self.again = discord.ui.Button(emoji="🔁", label="Replay")
        self.leave = discord.ui.Button(
            emoji="⏹", label="Stop", style=discord.ButtonStyle.danger
        )
        for btn, cb in (
            (self.prev, self.on_prev),
            (self.toggle, self.on_toggle),
            (self.nxt, self.on_next),
            (self.again, self.on_replay),
            (self.leave, self.on_stop),
        ):
            btn.callback = cb
            self.add_item(btn)

    def sync(self, guild: discord.Guild):
        """Re-point the controls at current state. Call before every redraw."""
        p = self.cog.player(self.guild_id)
        vc = guild.voice_client
        paused = bool(vc) and vc.is_paused()
        live = bool(vc) and (vc.is_playing() or paused)
        self.toggle.emoji = "▶" if paused else "⏸"
        self.toggle.label = "Resume" if paused else "Pause"
        self.toggle.disabled = not live
        self.prev.disabled = p.index <= 0
        self.nxt.disabled = p.index + 1 >= len(p.queue)
        self.again.disabled = p.current is None
        self.leave.disabled = vc is None

    # Each handler answers the click, then lets the resulting playback change
    # drive the redraw — jump_to routes through play_index, which refreshes.
    # Preconditions are re-checked here because a stale panel can still be
    # clicked even with the button disabled.
    async def on_prev(self, interaction: discord.Interaction):
        p = self.cog.player(self.guild_id)
        if p.index <= 0:
            return await interaction.response.send_message(
                "No previous song.", ephemeral=True
            )
        await interaction.response.defer()
        await self.cog.jump_to(interaction.guild, p.index - 1)

    async def on_next(self, interaction: discord.Interaction):
        p = self.cog.player(self.guild_id)
        if p.index + 1 >= len(p.queue):
            return await interaction.response.send_message(
                "Nothing left in the queue.", ephemeral=True
            )
        await interaction.response.defer()
        await self.cog.jump_to(interaction.guild, p.index + 1)

    async def on_replay(self, interaction: discord.Interaction):
        p = self.cog.player(self.guild_id)
        if not p.current:
            return await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
        await interaction.response.defer()
        await self.cog.jump_to(interaction.guild, p.index)

    async def on_toggle(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
        elif vc and vc.is_playing():
            vc.pause()
        else:
            return await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )
        await interaction.response.defer()
        await self.cog.refresh_panel(interaction.guild)

    async def on_stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message(
                "I'm not in a voice channel.", ephemeral=True
            )
        await interaction.response.defer()
        # on_voice_state_update closes the panel once the disconnect lands.
        await vc.disconnect()


class QueueView(discord.ui.View):
    def __init__(self, cog: "Music", guild: discord.Guild, selected: str | None = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.selected = selected  # track id, not a position
        self.msg: discord.Message | None = None  # set once sent, for on_timeout
        p = cog.player(guild.id)
        upcoming = [(i, t) for i, t in enumerate(p.queue) if i > p.index][:25]
        if upcoming:
            select = discord.ui.Select(
                placeholder="Select a track to move / remove",
                options=[
                    discord.SelectOption(
                        label=f"{i + 1}. {t['title']}"[:100],
                        value=t["id"],
                        default=t["id"] == selected,
                    )
                    for i, t in upcoming
                ],
            )
            select.callback = self.on_select
            self.add_item(select)
            for label, cb in (("⬆ Up", self.on_up), ("⬇ Down", self.on_down),
                              ("🗑 Remove", self.on_remove)):
                btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
                btn.callback = cb
                self.add_item(btn)
        clear = discord.ui.Button(label="Clear queue", style=discord.ButtonStyle.danger)
        clear.callback = self.on_clear
        self.add_item(clear)

    async def on_timeout(self):
        """Grey the controls out instead of leaving buttons that look live but
        answer with "This interaction failed"."""
        for item in self.children:
            item.disabled = True
        if self.msg:
            try:
                await self.msg.edit(view=self)
            except discord.HTTPException:
                pass  # message gone; nothing to grey out

    async def refresh(self, interaction: discord.Interaction, selected=None):
        view = QueueView(self.cog, self.guild, selected)
        view.msg = self.msg
        await interaction.response.edit_message(
            embed=self.cog.queue_embed(self.guild.id), view=view
        )
        self.stop()  # superseded: its timeout must not overwrite the new view
        await self.cog.refresh_panel(self.guild)  # "Up next" may have changed

    async def on_select(self, interaction: discord.Interaction):
        await self.refresh(interaction, interaction.data["values"][0])

    async def _resolve(self, interaction) -> int | None:
        """Position of the selected track right now, or None (with a reply sent)
        if nothing is selected or it left the queue since this view was built."""
        if self.selected is None:
            await interaction.response.send_message(
                "Select a track first.", ephemeral=True
            )
            return None
        i = self.cog.player(self.guild.id).find(self.selected)
        if i is None:
            await interaction.response.send_message(
                "That track is no longer in the queue.", ephemeral=True
            )
        return i

    async def _move(self, interaction, delta):
        i = await self._resolve(interaction)
        if i is None:
            return
        self.cog.player(self.guild.id).move(i, delta)
        # Selection follows the track by id, so it survives the reorder.
        await self.refresh(interaction, self.selected)

    async def on_up(self, interaction):
        await self._move(interaction, -1)

    async def on_down(self, interaction):
        await self._move(interaction, 1)

    async def on_remove(self, interaction):
        i = await self._resolve(interaction)
        if i is None:
            return
        self.cog.player(self.guild.id).remove(i)
        await self.refresh(interaction)

    async def on_clear(self, interaction):
        self.cog.player(self.guild.id).clear()
        await self.refresh(interaction)


@app_commands.guild_only()
class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}
        self.panels: dict[int, NowPlaying] = {}

    def player(self, guild_id: int) -> Player:
        return self.players.setdefault(guild_id, Player())

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.players.pop(guild.id, None)
        self.panels.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """Retire the panel when the bot leaves voice. Every exit routes through
        here — /stop, /exit, a moderator disconnect, a deleted channel — so this
        is the one place that needs to handle it."""
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel and not after.channel:
                await self.close_panel(member.guild)

    def panel_embed(self, guild: discord.Guild) -> discord.Embed:
        p = self.player(guild.id)
        vc = guild.voice_client
        cur = p.current
        left = len(p.queue) - p.index - 1
        if not cur:
            return discord.Embed(
                title="⏹ Nothing playing",
                description=(
                    f"**{left}** track(s) still queued — press ⏭ to start one."
                    if left > 0
                    else "The queue is empty. Use `/play` to add something."
                ),
            )
        state = "⏸ Paused" if (vc and vc.is_paused()) else "▶ Now playing"
        embed = discord.Embed(
            title=cur["title"][:256],
            url=cur.get("url"),
            description=f"{state} · {fmt_duration(cur.get('duration'))}",
        )
        if cur.get("uploader"):
            embed.add_field(name="Uploader", value=cur["uploader"][:1024])
        embed.add_field(name="Track", value=f"{p.index + 1} of {len(p.queue)}")
        if left > 0:
            nxt = p.queue[p.index + 1]
            embed.add_field(
                name="Up next",
                value=f"{nxt['title'][:80]} · {fmt_duration(nxt.get('duration'))}",
                inline=False,
            )
        embed.set_footer(text=f"{left} track(s) left in the queue")
        if cur.get("thumb"):
            embed.set_thumbnail(url=cur["thumb"])
        return embed

    async def ensure_panel(self, guild: discord.Guild, channel):
        """Post the now-playing panel if this guild hasn't got one, else redraw.

        Called from every /play path so the panel shows up as soon as the bot
        joins voice. Swallows send failures — a missing Send Messages permission
        should cost you the panel, not the music.
        """
        if guild.id in self.panels:
            return await self.refresh_panel(guild)
        view = NowPlaying(self, guild.id)
        view.sync(guild)
        try:
            view.msg = await channel.send(embed=self.panel_embed(guild), view=view)
        except Exception as err:
            print(f"Could not post the now-playing panel: {err}")
            return
        self.panels[guild.id] = view

    async def refresh_panel(self, guild: discord.Guild):
        """Redraw the panel if one is open.

        Never raises. This is called from playback paths — including via the
        FFmpeg `after` callback — where an exception would land in a Future
        nobody awaits and silently end the queue.
        """
        panel = self.panels.get(guild.id)
        if not panel or not panel.msg:
            return
        panel.sync(guild)
        try:
            await panel.msg.edit(embed=self.panel_embed(guild), view=panel)
        except Exception as err:
            print(f"Panel refresh failed: {err}")

    async def close_panel(self, guild: discord.Guild, note: str = "I left the voice channel."):
        """Retire the panel: controls removed, final state shown."""
        panel = self.panels.pop(guild.id, None)
        if not panel:
            return
        panel.stop()
        if not panel.msg:
            return
        try:
            await panel.msg.edit(
                embed=discord.Embed(title="⏹ Playback ended", description=note),
                view=None,
            )
        except Exception as err:
            print(f"Panel close failed: {err}")

    def queue_embed(self, guild_id: int) -> discord.Embed:
        p = self.player(guild_id)
        if not p.queue:
            return discord.Embed(title="Queue", description="The queue is empty.")
        lines = [
            # Titles come from yt-dlp; cap them so 25 of them can't overrun
            # Discord's 4096-char embed description limit.
            f"{i + 1}. {'▶ ' if i == p.index else ''}{t['title'][:100]}"
            for i, t in enumerate(p.queue)
            if p.index <= i < p.index + 25
        ]
        remaining = len(p.queue) - p.index - 25
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        return discord.Embed(title="Queue", description="\n".join(lines))

    async def play_index(self, guild: discord.Guild, i: int):
        """Play queue index i, skipping tracks yt-dlp can't resolve."""
        p = self.player(guild.id)

        def after(err):
            if err:
                print(f"Player error: {err}")
            asyncio.run_coroutine_threadsafe(self.advance(guild), self.bot.loop)

        async with p.lock:
            while 0 <= i < len(p.queue):
                vc = guild.voice_client
                if not vc or not vc.is_connected():
                    return
                if vc.is_playing() or vc.is_paused():
                    return  # another start got there first; don't double up
                track = p.queue[i]  # hold a ref; the queue may be edited mid-await
                p.index = i
                try:
                    info = await asyncio.to_thread(
                        YTDL_STREAM.extract_info, track["url"], download=False
                    )
                    url = info["url"]
                except (yt_dlp.utils.DownloadError, KeyError) as err:
                    # Unavailable / private / geo-blocked: move on, don't stall.
                    print(f"Skipping {track['title']}: {err}")
                    i += 1
                    continue
                vc.play(discord.FFmpegPCMAudio(url, **FFMPEG_OPTS), after=after)
                await self.refresh_panel(guild)  # new track on screen
                return
        # Fell off the end of the queue without playing anything.
        await self.refresh_panel(guild)

    async def advance(self, guild: discord.Guild):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        i = self.player(guild.id).step()
        if i is None:
            await self.refresh_panel(guild)  # queue ran out; show the idle state
            return
        await self.play_index(guild, i)

    async def jump_to(self, guild: discord.Guild, i: int) -> bool:
        """Move playback to queue index i. False if we aren't connected.

        Shared by the slash commands and the now-playing buttons.
        """
        vc = guild.voice_client
        if not vc:
            return False
        if vc.is_playing() or vc.is_paused():
            self.player(guild.id).jump = i
            vc.stop()  # after-callback fires advance(), which honours the jump
        else:
            await self.play_index(guild, i)
        return True

    async def _jump(self, interaction: discord.Interaction, i: int, ok_msg: str):
        """Jump to queue index i, or say why we can't. /stop leaves the queue
        intact but drops the voice client, so there is nothing to jump in."""
        if not interaction.guild.voice_client:
            return await interaction.response.send_message(
                "I'm not in a voice channel — use /play to start."
            )
        # Reply first: Discord drops an interaction with no response inside 3s,
        # and jump_to below can spend longer than that resolving a stream.
        await interaction.response.send_message(ok_msg)
        await self.jump_to(interaction.guild, i)

    async def enqueue(
        self, guild: discord.Guild, tracks: list[dict], *, ahead: bool
    ) -> bool:
        """Add tracks and start playback if nothing is playing. Returns whether
        playback started as a result.

        `ahead` marks one explicitly requested song: when idle it is inserted at
        index + 1 so it plays now. Appending instead would walk the index past
        anything still pending, and both view layers filter on the index
        (`queue_embed` uses `p.index <= i`, `QueueView` uses `i > p.index`), so
        those tracks would vanish from /queue and /next. Playlists pass
        ahead=False and always append, leaving pending tracks in front of them.
        """
        p = self.player(guild.id)
        vc = guild.voice_client
        idle = bool(vc) and not (vc.is_playing() or vc.is_paused())
        if ahead and idle:
            p.queue.insert(p.index + 1, tracks[0])
        else:
            p.queue.extend(tracks)
        if idle:
            # index + 1 is the oldest unplayed track: what was just inserted,
            # or whatever was already pending. On a played-through queue it is
            # the first appended track, so one expression covers every case.
            await self.play_index(guild, p.index + 1)
        return idle

    async def load_rest(
        self, guild: discord.Guild, query: str, start_vid: str | None
    ) -> int:
        """Append the playlist entries that follow `start_vid`, returning how many.

        Runs after playback already started on that track, so a long playlist
        enumerates while music plays instead of before it. Slicing from the
        linked song rather than from entry 1 keeps the order the link implied:
        paste song 40 of a playlist and you get 40 onward, as YouTube does.
        """
        p = self.player(guild.id)
        info = await asyncio.to_thread(YTDL_FULL.extract_info, query, download=False)
        if self.players.get(guild.id) is not p:
            return 0  # /exit or a guild removal landed while we were loading
        tracks = tracks_from(info)
        # Falls back to 0 (i.e. start at entry 1) if the track isn't in the list.
        at = next(
            (n for n, t in enumerate(tracks) if start_vid and t.get("vid") == start_vid),
            0,
        )
        tracks = tracks[at + 1 :]
        if not tracks:
            return 0
        p.queue.extend(tracks)
        vc = guild.voice_client
        if vc and not (vc.is_playing() or vc.is_paused()):
            # Entry 1 can finish before a long playlist finishes loading, which
            # leaves advance() having found an empty queue. Restart from here.
            await self.play_index(guild, p.index + 1)
        await self.refresh_panel(guild)  # queue count and "Up next" both moved
        return len(tracks)

    @app_commands.command(description="Play a song, YouTube link, or playlist link")
    @app_commands.describe(query="Song name, YouTube video link, or playlist link")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        if not vc:
            if not (interaction.user.voice and interaction.user.voice.channel):
                return await interaction.followup.send("Join a voice channel first.")
            vc = await interaction.user.voice.channel.connect()
        try:
            info = await asyncio.to_thread(
                YTDL_PROBE.extract_info, query, download=False
            )
        except Exception as err:
            # Broad on purpose: the interaction is deferred, so any escaping
            # exception would leave the user staring at "thinking…" forever.
            print(f"Extraction failed for {query!r}: {err}")
            info = None
        if not info:
            return await interaction.followup.send("No results found.")
        if is_playlist(info):
            if not tracks_from(info):
                return await interaction.followup.send("That playlist looks empty.")
            title = (info.get("title") or "Playlist")[:100]
            view = PlaylistPrompt(self, interaction.user.id, query, title)
            view.msg = await interaction.followup.send(
                f"**{title}** is a playlist. Add all of it, or just one song?",
                view=view,
                wait=True,  # without this followup.send returns None
            )
            return
        tracks = tracks_from(info)
        if not tracks:
            return await interaction.followup.send("No results found.")
        started = await self.enqueue(interaction.guild, tracks, ahead=True)
        verb = "Playing" if started else "Queued"
        await interaction.followup.send(f"{verb} **{tracks[0]['title']}**")
        await self.ensure_panel(interaction.guild, interaction.channel)

    @app_commands.command(
        name="nowplaying", description="Show the now playing panel with controls"
    )
    async def nowplaying(self, interaction: discord.Interaction):
        if not interaction.guild.voice_client:
            return await interaction.response.send_message(
                "I'm not in a voice channel — use /play to start."
            )
        # One live panel per guild, or several would compete to be the truth.
        await self.close_panel(interaction.guild, "Replaced by a newer panel.")
        view = NowPlaying(self, interaction.guild_id)
        view.sync(interaction.guild)
        await interaction.response.send_message(
            embed=self.panel_embed(interaction.guild), view=view
        )
        view.msg = await interaction.original_response()
        self.panels[interaction.guild_id] = view

    @app_commands.command(description="Show the queue and move or remove tracks")
    async def queue(self, interaction: discord.Interaction):
        view = QueueView(self, interaction.guild)
        await interaction.response.send_message(
            embed=self.queue_embed(interaction.guild_id), view=view
        )
        view.msg = await interaction.original_response()

    @app_commands.command(description="Skip to the next song")
    async def next(self, interaction: discord.Interaction):
        p = self.player(interaction.guild_id)
        if p.index + 1 >= len(p.queue):
            return await interaction.response.send_message("Nothing left in the queue.")
        await self._jump(interaction, p.index + 1, "Skipped.")

    @app_commands.command(description="Skip back to the previous song")
    async def back(self, interaction: discord.Interaction):
        p = self.player(interaction.guild_id)
        if p.index <= 0:
            return await interaction.response.send_message("No previous song.")
        await self._jump(interaction, p.index - 1, "Playing previous song.")

    @app_commands.command(description="Replay the current song from the start")
    async def replay(self, interaction: discord.Interaction):
        p = self.player(interaction.guild_id)
        if not p.current:
            return await interaction.response.send_message("Nothing is playing.")
        await self._jump(interaction, p.index, f"Replaying **{p.current['title']}**.")

    @app_commands.command(description="Pause the music")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.")
        vc.pause()
        await interaction.response.send_message("Paused.")
        await self.refresh_panel(interaction.guild)

    @app_commands.command(description="Resume the music")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("Nothing is paused.")
        vc.resume()
        await interaction.response.send_message("Resumed.")
        await self.refresh_panel(interaction.guild)

    @app_commands.command(description="Leave the voice channel (queue is kept)")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("I'm not in a voice channel.")
        await vc.disconnect()
        await interaction.response.send_message("Stopped. Queue kept.")

    @app_commands.command(description="Clear the queue")
    async def clear(self, interaction: discord.Interaction):
        self.player(interaction.guild_id).clear()
        await interaction.response.send_message("Queue cleared.")
        await self.refresh_panel(interaction.guild)

    @app_commands.command(description="Leave the voice channel and clear the queue")
    async def exit(self, interaction: discord.Interaction):
        self.players.pop(interaction.guild_id, None)
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message("Bye! Queue cleared.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
