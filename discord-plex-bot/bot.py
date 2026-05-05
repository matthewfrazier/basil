"""Discord music bot that streams from a Plex music library.

Run: python bot.py
Requires: ffmpeg on PATH, a Discord bot token, and Plex credentials (see .env.example).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from plexapi.audio import Track
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("plex-discord-bot")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
PLEX_BASE_URL = os.getenv("PLEX_BASE_URL")
PLEX_TOKEN = os.getenv("PLEX_TOKEN")
PLEX_USERNAME = os.getenv("PLEX_USERNAME")
PLEX_PASSWORD = os.getenv("PLEX_PASSWORD")
PLEX_SERVER_NAME = os.getenv("PLEX_SERVER_NAME")
PLEX_MUSIC_SECTION = os.getenv("PLEX_MUSIC_SECTION", "Music")

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


def connect_plex() -> PlexServer:
    if PLEX_BASE_URL and PLEX_TOKEN:
        return PlexServer(PLEX_BASE_URL, PLEX_TOKEN)
    if PLEX_USERNAME and PLEX_PASSWORD and PLEX_SERVER_NAME:
        account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)
        return account.resource(PLEX_SERVER_NAME).connect()
    raise RuntimeError("Set PLEX_BASE_URL+PLEX_TOKEN or PLEX_USERNAME+PLEX_PASSWORD+PLEX_SERVER_NAME")


@dataclass
class QueuedTrack:
    track: Track
    requested_by: str

    @property
    def title(self) -> str:
        artist = getattr(self.track, "grandparentTitle", "Unknown Artist")
        album = getattr(self.track, "parentTitle", "")
        suffix = f" ({album})" if album else ""
        return f"{artist} – {self.track.title}{suffix}"


class GuildPlayer:
    """Per-guild playback state: voice client, queue, and play loop."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[QueuedTrack] = deque()
        self.current: Optional[QueuedTrack] = None
        self.voice: Optional[discord.VoiceClient] = None
        self.next_event = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    async def ensure_connected(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        if self.voice and self.voice.is_connected():
            if self.voice.channel != channel:
                await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect(self_deaf=True)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._player_loop())
        return self.voice

    def enqueue(self, item: QueuedTrack) -> None:
        self.queue.append(item)

    def _on_finished(self, error: Optional[Exception]) -> None:
        if error:
            log.error("Playback error: %s", error)
        self.bot.loop.call_soon_threadsafe(self.next_event.set)

    async def _player_loop(self) -> None:
        try:
            while True:
                self.next_event.clear()
                if not self.queue:
                    try:
                        await asyncio.wait_for(self._wait_for_track(), timeout=300)
                    except asyncio.TimeoutError:
                        break
                self.current = self.queue.popleft()
                stream_url = await asyncio.to_thread(self.current.track.getStreamURL)
                source = discord.FFmpegPCMAudio(
                    stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTIONS
                )
                if not self.voice or not self.voice.is_connected():
                    break
                self.voice.play(source, after=self._on_finished)
                await self.next_event.wait()
                self.current = None
        finally:
            self.current = None
            if self.voice and self.voice.is_connected():
                await self.voice.disconnect()
            self.voice = None

    async def _wait_for_track(self) -> None:
        while not self.queue:
            await asyncio.sleep(1)

    def skip(self) -> bool:
        if self.voice and self.voice.is_playing():
            self.voice.stop()
            return True
        return False

    async def stop(self) -> None:
        self.queue.clear()
        if self.voice:
            self.voice.stop()
            await self.voice.disconnect()
        self.voice = None


class PlexBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.plex: Optional[PlexServer] = None
        self.players: dict[int, GuildPlayer] = {}

    async def setup_hook(self) -> None:
        self.plex = await asyncio.to_thread(connect_plex)
        log.info("Connected to Plex: %s", self.plex.friendlyName)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    def player_for(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = GuildPlayer(self, guild)
            self.players[guild.id] = player
        return player

    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        assert self.plex is not None
        section = self.plex.library.section(PLEX_MUSIC_SECTION)
        results = section.searchTracks(title=query, maxresults=limit)
        if not results:
            results = [
                t for t in section.search(query, libtype="track", limit=limit) if isinstance(t, Track)
            ]
        return results


bot = PlexBot()


def author_voice_channel(interaction: discord.Interaction) -> Optional[discord.VoiceChannel]:
    member = interaction.user
    if isinstance(member, discord.Member) and member.voice and member.voice.channel:
        channel = member.voice.channel
        if isinstance(channel, discord.VoiceChannel):
            return channel
    return None


@bot.tree.command(name="play", description="Search Plex and play a track in your voice channel")
@app_commands.describe(query="Track title, artist, or album to search for")
async def play(interaction: discord.Interaction, query: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    channel = author_voice_channel(interaction)
    if channel is None:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    tracks = await asyncio.to_thread(bot.search_tracks, query, 1)
    if not tracks:
        await interaction.followup.send(f"No results for **{query}**.")
        return

    item = QueuedTrack(track=tracks[0], requested_by=interaction.user.display_name)
    player = bot.player_for(interaction.guild)
    await player.ensure_connected(channel)
    player.enqueue(item)

    position = len(player.queue)
    if player.current is None and position == 1:
        await interaction.followup.send(f"Now playing: **{item.title}**")
    else:
        await interaction.followup.send(f"Queued (#{position}): **{item.title}**")


@bot.tree.command(name="search", description="Search Plex and pick from the top results")
@app_commands.describe(query="What to search for")
async def search(interaction: discord.Interaction, query: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    tracks = await asyncio.to_thread(bot.search_tracks, query, 10)
    if not tracks:
        await interaction.followup.send(f"No results for **{query}**.")
        return
    lines = [
        f"{i + 1}. {getattr(t, 'grandparentTitle', '?')} – {t.title}"
        for i, t in enumerate(tracks)
    ]
    await interaction.followup.send(
        "Top results (use `/play` with a more specific query to pick one):\n" + "\n".join(lines)
    )


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.player_for(interaction.guild)
    lines: list[str] = []
    if player.current:
        lines.append(f"**Now:** {player.current.title} (req {player.current.requested_by})")
    for i, item in enumerate(player.queue, start=1):
        lines.append(f"{i}. {item.title} (req {item.requested_by})")
    await interaction.response.send_message("\n".join(lines) if lines else "Queue is empty.")


@bot.tree.command(name="skip", description="Skip the current track")
async def skip(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.player_for(interaction.guild)
    if player.skip():
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.player_for(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.player_for(interaction.guild)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Not paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback, clear the queue, and disconnect")
async def stop(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.player_for(interaction.guild)
    await player.stop()
    await interaction.response.send_message("Stopped and disconnected.")


@bot.tree.command(name="nowplaying", description="Show the current track")
async def nowplaying(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    player = bot.player_for(interaction.guild)
    if player.current:
        await interaction.response.send_message(f"Now playing: **{player.current.title}**")
    else:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
