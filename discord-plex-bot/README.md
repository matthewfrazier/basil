# Plex → Discord Music Bot

Free, self-hosted Discord bot that streams music from your Plex library into a Discord voice channel using slash commands.

## Stack

- [discord.py](https://github.com/Rapptz/discord.py) (voice support)
- [python-plexapi](https://github.com/pkkid/python-plexapi)
- ffmpeg (system dependency)

Everything is open-source and free. Hosting cost = zero if you run it on the same box as Plex.

## Prerequisites

- Python 3.10+
- ffmpeg on `PATH` (`apt install ffmpeg` / `brew install ffmpeg`)
- A Discord application + bot token: <https://discord.com/developers/applications>
  - Enable the **bot** scope and the **Connect** + **Speak** voice permissions when generating the invite URL.
  - No privileged intents are required.
- Access to a Plex server with a **Music** library section.
  - Easiest auth: a Plex token. See <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>.

## Setup

```bash
cd discord-plex-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your tokens / Plex URL
python bot.py
```

For instant slash-command updates while developing, set `DISCORD_GUILD_ID` to your test server's ID. Leave it blank for global commands (can take ~1 hour to propagate).

## Commands

| Command | Description |
|---|---|
| `/play <query>` | Search Plex and play the top hit. Joins your voice channel. |
| `/search <query>` | List the top 10 matches without queuing. |
| `/queue` | Show the queue. |
| `/skip` | Skip the current track. |
| `/pause` / `/resume` | Toggle playback. |
| `/stop` | Clear the queue and disconnect. |
| `/nowplaying` | Show the current track. |

## How it works

`PlexServer.searchTracks()` finds matching tracks; `track.getStreamURL()` returns a tokenized HTTP URL that ffmpeg streams directly to Discord via `discord.FFmpegPCMAudio`. Each guild gets its own `GuildPlayer` with an asyncio queue and a single play loop, so multiple servers can use the bot concurrently.

## Notes / limits

- Plex `getStreamURL` issues a transcode-friendly link; for direct play of MP3/FLAC the bytes pass through unchanged.
- The bot auto-disconnects after 5 minutes of an empty queue.
- "Shared" libraries: as long as the Plex token you provide can see the music section, the bot can play it. For a friend's library, log into their server through your Plex account and use that token.
- This is not a YouTube/Spotify bot — it only plays what's in the Plex library, which keeps it on the right side of Discord's TOS for music bots.
