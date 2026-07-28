import os
import pkgutil

import discord
from discord.ext import commands


class Bot(commands.Bot):
    async def setup_hook(self):
        for mod in pkgutil.iter_modules(["packages"]):
            await self.load_extension(f"packages.{mod.name}")
            print(f"Loaded package: {mod.name}")
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
    Bot(command_prefix="!", intents=discord.Intents.default()).run(
        os.environ["DISCORD_TOKEN"]
    )
