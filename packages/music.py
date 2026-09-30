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
# Everything after the first entry, fetched once playback is already going.
YTDL_REST = yt_dlp.YoutubeDL({**YTDL_OPTS, "playlist_items": "2:"})
YTDL_STREAM = yt_dlp.YoutubeDL(
    {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
)


def tracks_from(info: dict) -> list[dict]:
    """Queue entries from a yt-dlp result, skipping anything unplayable."""
    tracks = []
    for e in info.get("entries") or [info]:
        if not e:
            continue
        url = e.get("url") or e.get("webpage_url")
        if not url and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url:
            continue  # nothing playable; skip rather than queue a bad entry
        tracks.append(
            {
                # Stable handle for the queue UI: positions shift, ids don't.
                "id": uuid.uuid4().hex,
                "title": e.get("title") or "Unknown",
                "url": url,
            }
        )
    return tracks


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

    def __init__(self, cog: "Music", user_id: int, query: str, title: str,
                 first: list[dict]):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id  # only the requester may answer
        self.query = query
        self.title = title
        self.first = first  # playlist entry 1, already extracted by the probe
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

    async def on_all(self, interaction: discord.Interaction):
        await self._claim(interaction, f"Loading **{self.title}**…")
        # Entry 1 first so playback can start now; the rest follows while it plays.
        await self.cog.enqueue(interaction.guild, self.first, ahead=False)
        try:
            rest = await self.cog.load_rest(interaction.guild, self.query)
        except Exception as err:
            print(f"Playlist load failed for {self.query!r}: {err}")
            return await self._say(
                interaction,
                f"Queued **{len(self.first)}** track from **{self.title}** — "
                "couldn't load the rest.",
            )
        total = len(self.first) + rest
        await self._say(
            interaction, f"Queued **{total}** tracks from **{self.title}**."
        )

    async def on_one(self, interaction: discord.Interaction):
        await self._claim(interaction, "Loading…")
        try:
            info = await asyncio.to_thread(
                YTDL_SOLO.extract_info, self.query, download=False
            )
            tracks = tracks_from(info)[:1]
        except Exception as err:
            print(f"Extraction failed for {self.query!r}: {err}")
            tracks = []
        if not tracks:
            return await self._say(interaction, "Couldn't load that song.")
        started = await self.cog.enqueue(interaction.guild, tracks, ahead=True)
        verb = "Playing" if started else "Queued"
        await self._say(interaction, f"{verb} **{tracks[0]['title']}**")


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

    def player(self, guild_id: int) -> Player:
        return self.players.setdefault(guild_id, Player())

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.players.pop(guild.id, None)

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
                return

    async def advance(self, guild: discord.Guild):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        i = self.player(guild.id).step()
        if i is not None:
            await self.play_index(guild, i)

    async def _jump(self, interaction: discord.Interaction, i: int, ok_msg: str):
        """Jump to queue index i, or say why we can't. /stop leaves the queue
        intact but drops the voice client, so there is nothing to jump in."""
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message(
                "I'm not in a voice channel — use /play to start."
            )
        # Reply first: Discord drops an interaction with no response inside 3s,
        # and play_index below can spend longer than that resolving a stream.
        await interaction.response.send_message(ok_msg)
        if vc.is_playing() or vc.is_paused():
            self.player(interaction.guild_id).jump = i
            vc.stop()  # after-callback fires advance(), which honours the jump
        else:
            await self.play_index(interaction.guild, i)

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

    async def load_rest(self, guild: discord.Guild, query: str) -> int:
        """Append a playlist's entries after the first, returning how many.

        Runs after playback has already started on entry 1, so a few-hundred
        song playlist enumerates while music plays instead of before it.
        """
        p = self.player(guild.id)
        info = await asyncio.to_thread(YTDL_REST.extract_info, query, download=False)
        if self.players.get(guild.id) is not p:
            return 0  # /exit or a guild removal landed while we were loading
        tracks = tracks_from(info)
        if not tracks:
            return 0
        p.queue.extend(tracks)
        vc = guild.voice_client
        if vc and not (vc.is_playing() or vc.is_paused()):
            # Entry 1 can finish before a long playlist finishes loading, which
            # leaves advance() having found an empty queue. Restart from here.
            await self.play_index(guild, p.index + 1)
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
            first = tracks_from(info)
            if not first:
                return await interaction.followup.send("That playlist looks empty.")
            title = (info.get("title") or "Playlist")[:100]
            view = PlaylistPrompt(self, interaction.user.id, query, title, first)
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

    @app_commands.command(description="Resume the music")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_paused():
            return await interaction.response.send_message("Nothing is paused.")
        vc.resume()
        await interaction.response.send_message("Resumed.")

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

    @app_commands.command(description="Leave the voice channel and clear the queue")
    async def exit(self, interaction: discord.Interaction):
        self.players.pop(interaction.guild_id, None)
        vc = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
        await interaction.response.send_message("Bye! Queue cleared.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
