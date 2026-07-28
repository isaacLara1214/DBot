import asyncio
import functools

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
YTDL_FLAT = yt_dlp.YoutubeDL(
    {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "extract_flat": "in_playlist",
    }
)
YTDL_STREAM = yt_dlp.YoutubeDL(
    {"format": "bestaudio/best", "quiet": True, "no_warnings": True}
)


def extract_tracks(query: str) -> list[dict]:
    info = YTDL_FLAT.extract_info(query, download=False)
    entries = info.get("entries") or [info]
    return [
        {
            "title": e.get("title") or "Unknown",
            "url": e.get("url")
            or e.get("webpage_url")
            or f"https://www.youtube.com/watch?v={e['id']}",
        }
        for e in entries
        if e
    ]


class Player:
    """Per-guild queue. Played tracks stay in the list so /back and /replay work;
    index points at the current track."""

    def __init__(self):
        self.queue: list[dict] = []
        self.index = -1
        self.jump: int | None = None

    @property
    def current(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def step(self) -> int | None:
        """Next index to play when a track ends (honours a pending jump)."""
        i = self.jump if self.jump is not None else self.index + 1
        self.jump = None
        return i if 0 <= i < len(self.queue) else None

    def clear(self):
        cur = self.current
        self.queue = [cur] if cur else []
        self.index = 0 if cur else -1
        self.jump = None

    def move(self, i: int, delta: int) -> bool:
        j = i + delta
        if self.index < min(i, j) and max(i, j) < len(self.queue):
            self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
            return True
        return False

    def remove(self, i: int) -> bool:
        if self.index < i < len(self.queue):
            self.queue.pop(i)
            return True
        return False


class QueueView(discord.ui.View):
    def __init__(self, cog: "Music", guild: discord.Guild, selected: int | None = None):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.selected = selected
        p = cog.player(guild.id)
        upcoming = [(i, t) for i, t in enumerate(p.queue) if i > p.index][:25]
        if upcoming:
            select = discord.ui.Select(
                placeholder="Select a track to move / remove",
                options=[
                    discord.SelectOption(
                        label=f"{i + 1}. {t['title']}"[:100],
                        value=str(i),
                        default=i == selected,
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

    async def refresh(self, interaction: discord.Interaction, selected=None):
        await interaction.response.edit_message(
            embed=self.cog.queue_embed(self.guild.id),
            view=QueueView(self.cog, self.guild, selected),
        )

    async def on_select(self, interaction: discord.Interaction):
        await self.refresh(interaction, int(interaction.data["values"][0]))

    async def _move(self, interaction, delta):
        if self.selected is None:
            return await interaction.response.send_message(
                "Select a track first.", ephemeral=True
            )
        p = self.cog.player(self.guild.id)
        moved = p.move(self.selected, delta)
        await self.refresh(interaction, self.selected + delta if moved else self.selected)

    async def on_up(self, interaction):
        await self._move(interaction, -1)

    async def on_down(self, interaction):
        await self._move(interaction, 1)

    async def on_remove(self, interaction):
        if self.selected is None:
            return await interaction.response.send_message(
                "Select a track first.", ephemeral=True
            )
        self.cog.player(self.guild.id).remove(self.selected)
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

    def queue_embed(self, guild_id: int) -> discord.Embed:
        p = self.player(guild_id)
        if not p.queue:
            return discord.Embed(title="Queue", description="The queue is empty.")
        lines = [
            f"{i + 1}. {'▶ ' if i == p.index else ''}{t['title']}"
            for i, t in enumerate(p.queue)
            if p.index <= i < p.index + 25
        ]
        remaining = len(p.queue) - p.index - 25
        if remaining > 0:
            lines.append(f"… and {remaining} more")
        return discord.Embed(title="Queue", description="\n".join(lines))

    async def play_index(self, guild: discord.Guild, i: int):
        p = self.player(guild.id)
        vc = guild.voice_client
        if not vc or not (0 <= i < len(p.queue)):
            return
        p.index = i
        info = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(YTDL_STREAM.extract_info, p.queue[i]["url"], download=False),
        )

        def after(err):
            if err:
                print(f"Player error: {err}")
            asyncio.run_coroutine_threadsafe(self.advance(guild), self.bot.loop)

        vc.play(discord.FFmpegPCMAudio(info["url"], **FFMPEG_OPTS), after=after)

    async def advance(self, guild: discord.Guild):
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return
        i = self.player(guild.id).step()
        if i is not None:
            await self.play_index(guild, i)

    async def skip_to(self, guild: discord.Guild, i: int):
        p = self.player(guild.id)
        vc = guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            p.jump = i
            vc.stop()  # after-callback fires advance(), which honours the jump
        else:
            await self.play_index(guild, i)

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
            tracks = await asyncio.get_running_loop().run_in_executor(
                None, extract_tracks, query
            )
        except yt_dlp.utils.DownloadError:
            tracks = []
        if not tracks:
            return await interaction.followup.send("No results found.")
        p = self.player(interaction.guild_id)
        start = len(p.queue)
        p.queue.extend(tracks)
        if not (vc.is_playing() or vc.is_paused()):
            await self.play_index(interaction.guild, start)
        msg = (
            f"Queued **{tracks[0]['title']}**"
            if len(tracks) == 1
            else f"Queued **{len(tracks)}** tracks"
        )
        await interaction.followup.send(msg)

    @app_commands.command(description="Show the queue and move or remove tracks")
    async def queue(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.queue_embed(interaction.guild_id),
            view=QueueView(self, interaction.guild),
        )

    @app_commands.command(description="Skip to the next song")
    async def next(self, interaction: discord.Interaction):
        p = self.player(interaction.guild_id)
        if p.index + 1 >= len(p.queue):
            return await interaction.response.send_message("Nothing left in the queue.")
        await self.skip_to(interaction.guild, p.index + 1)
        await interaction.response.send_message("Skipped.")

    @app_commands.command(description="Skip back to the previous song")
    async def back(self, interaction: discord.Interaction):
        p = self.player(interaction.guild_id)
        if p.index <= 0:
            return await interaction.response.send_message("No previous song.")
        await self.skip_to(interaction.guild, p.index - 1)
        await interaction.response.send_message("Playing previous song.")

    @app_commands.command(description="Replay the current song from the start")
    async def replay(self, interaction: discord.Interaction):
        p = self.player(interaction.guild_id)
        if not p.current:
            return await interaction.response.send_message("Nothing is playing.")
        await self.skip_to(interaction.guild, p.index)
        await interaction.response.send_message(f"Replaying **{p.current['title']}**.")

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
