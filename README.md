# Discord Bot

Package-based Discord bot. Drop a Python file into `packages/` and it's
loaded on startup — packages are [discord.py extensions](https://discordpy.readthedocs.io/en/stable/ext/commands/extensions.html)
(a file with an `async def setup(bot)` that adds a Cog). Packages are
isolated: one failing to load doesn't touch the others' functionality.

## Setup

1. Create an app at https://discord.com/developers/applications, add a Bot,
   copy the token.
2. Invite it: OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
   permissions `Connect`, `Speak`, `Send Messages`.
3. `cp .env.example .env` and fill in `DISCORD_TOKEN` (and `GUILD_ID` for
   instant command registration in your server).

## Run

```sh
docker compose up -d --build
```

## Packages

### music

| Command | Does |
|---|---|
| `/play <query>` | Song name (YouTube search), video link, or playlist link. Playlists append to the queue. |
| `/queue` | Show queue; move, remove, or clear tracks via buttons. |
| `/next` / `/back` | Skip forward / back. |
| `/replay` | Restart the current song. |
| `/pause` / `/resume` | Pause / resume. |
| `/stop` | Leave voice, keep the queue. |
| `/clear` | Clear the queue. |
| `/exit` | Leave voice and clear the queue. |

## Tests

```sh
python test_music.py
```
