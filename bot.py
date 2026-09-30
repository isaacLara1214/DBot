import os
import pkgutil
import traceback

import discord
from discord.ext import commands


class Bot(commands.Bot):
    async def setup_hook(self):
        for mod in pkgutil.iter_modules(["packages"]):
            # Broad catch on purpose: one bad package must not stop the others.
            try:
                await self.load_extension(f"packages.{mod.name}")
                print(f"Loaded package: {mod.name}")
            except Exception:
                print(f"Failed to load package: {mod.name}")
                traceback.print_exc()
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user}")


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        # docker-compose restarts on failure, so a traceback here would loop
        # unreadably. Say what to fix instead.
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    # command_prefix is unused (no prefix commands, and reading message content
    # would need a privileged intent) but commands.Bot requires it.
    Bot(command_prefix="!", intents=discord.Intents.default()).run(token)
