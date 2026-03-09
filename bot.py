import os
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp

# Optional keep_alive (for Replit/hosting). If missing, don't crash locally.
try:
    from keep_alive import keep_alive
except Exception:
    keep_alive = None


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

intents = discord.Intents.default()
# Not needed for slash commands; leave False to reduce permissions
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)  # prefix unused, but Bot gives us .tree nicely


@bot.event
async def on_ready():
    log = logging.getLogger(__name__)
    log.info("%s has connected to Discord!", bot.user)
    try:
        cmds = await bot.tree.sync()
        log.info("Synced %d command(s).", len(cmds))
    except Exception as exc:
        log.exception("Slash command sync failed: %s", exc)


YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "nocheckcertificate": True,
    "user_agent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    "youtube_include_dash_manifest": False,
    "cachedir": False,
    "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
}

if os.path.exists("cookies.txt"):
    YDL_OPTIONS["cookiefile"] = "cookies.txt"

_WINDOWS_FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFMPEG_PATH = _WINDOWS_FFMPEG if os.path.exists(_WINDOWS_FFMPEG) else "ffmpeg"

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 "
                      "-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5 "
                      "-probesize 10M -analyzeduration 10M "
                      '-user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"',
    "options": "-vn -buffer_size 10M",
}

_queues: dict[int, asyncio.Queue] = {}
_players: dict[int, asyncio.Task] = {}


def _log_play_error(error: Exception | None) -> None:
    if error:
        logging.getLogger(__name__).error("Player error: %s", error)


async def _send_interaction(interaction: discord.Interaction, message: str | None = None, embed: discord.Embed | None = None, ephemeral: bool = False):
    """
    Helper that safely responds/followups to interactions.
    """
    if interaction.response.is_done():
        return await interaction.followup.send(content=message, embed=embed, ephemeral=ephemeral)
    await interaction.response.send_message(content=message, embed=embed, ephemeral=ephemeral)
    return await interaction.original_response()


def _get_queue(guild_id: int) -> asyncio.Queue:
    if guild_id not in _queues:
        _queues[guild_id] = asyncio.Queue()
    return _queues[guild_id]


async def _extract_info(url: str) -> dict:
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception:
                temp_options = YDL_OPTIONS.copy()
                temp_options["user_agent"] = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
                if "extractor_args" in temp_options:
                    del temp_options["extractor_args"]
                with yt_dlp.YoutubeDL(temp_options) as ydl2:
                    info = ydl2.extract_info(url, download=False)

            if "entries" in info:
                info = info["entries"][0]

            return {
                "url": info["url"],
                "title": info.get("title", "Unknown"),
                "headers": info.get("http_headers", {}),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url"),
            }

    return await loop.run_in_executor(None, _extract)


async def _player_loop(guild: discord.Guild) -> None:
    queue = _get_queue(guild.id)
    while True:
        info, interaction = await queue.get()
        try:
            voice_client = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                continue

            audio_url = info["url"]
            headers = info["headers"]
            title = info["title"]

            before_options = FFMPEG_OPTIONS["before_options"]
            if headers:
                filtered_headers = {k: v for k, v in headers.items() if k.lower() not in ["user-agent", "host"]}
                header_str = "".join(f"{k}: {v}\r\n" for k, v in filtered_headers.items())
                if header_str:
                    before_options = f'{before_options} -headers "{header_str}"'

            voice_client.play(
                discord.FFmpegPCMAudio(
                    audio_url,
                    executable=FFMPEG_PATH,
                    before_options=before_options,
                    options=FFMPEG_OPTIONS["options"],
                ),
                after=_log_play_error,
            )

            embed = discord.Embed(
                title="Now Playing",
                description=f"[{title}]({info.get('webpage_url')})",
                color=discord.Color.blue(),
            )
            if info.get("thumbnail"):
                embed.set_thumbnail(url=info["thumbnail"])

            duration = info.get("duration")
            if duration:
                minutes, seconds = divmod(duration, 60)
                embed.add_field(name="Duration", value=f"{minutes:02d}:{seconds:02d}", inline=True)

            embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)

            # Send now playing in the same channel the command was used
            if interaction.channel:
                await interaction.channel.send(embed=embed)

            while voice_client.is_playing() or voice_client.is_paused():
                await asyncio.sleep(0.5)
        finally:
            queue.task_done()


async def _ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    if interaction.guild is None:
        await _send_interaction(interaction, "This command only works in a server.", ephemeral=True)
        return None

    if not isinstance(interaction.user, discord.Member):
        await _send_interaction(interaction, "Cannot resolve your member info.", ephemeral=True)
        return None

    if not interaction.user.voice:
        await _send_interaction(interaction, "You are not in a voice channel.", ephemeral=True)
        return None

    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client and voice_client.is_connected():
        if voice_client.channel != channel:
            await voice_client.move_to(channel)
    else:
        await channel.connect()

    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await _send_interaction(interaction, "Error connecting to the voice channel.", ephemeral=True)
        return None

    return voice_client


async def _play_core(interaction: discord.Interaction, url: str) -> None:
    await interaction.response.defer(ephemeral=True)

    voice_client = await _ensure_voice(interaction)
    if voice_client is None or interaction.guild is None:
        return

    status_msg = None
    try:
        status_msg = await interaction.followup.send("🔍 Searching...", ephemeral=True)
        info = await _extract_info(url)
    except Exception as exc:
        logging.getLogger(__name__).exception("yt-dlp extract failed: %s", exc)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await interaction.followup.send("Error retrieving audio information. Please try another URL.", ephemeral=True)
        return

    queue = _get_queue(voice_client.guild.id)
    was_idle = (not voice_client.is_playing()) and queue.empty()
    await queue.put((info, interaction))

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if not was_idle:
        embed = discord.Embed(
            title="Added to queue",
            description=f"[{info['title']}]({info.get('webpage_url')})",
            color=discord.Color.green(),
        )
        if info.get("thumbnail"):
            embed.set_thumbnail(url=info["thumbnail"])
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(f"Reproduciendo: **{info['title']}**", ephemeral=True)

    if voice_client.guild.id not in _players or _players[voice_client.guild.id].done():
        _players[voice_client.guild.id] = asyncio.create_task(_player_loop(voice_client.guild))


async def _pause_core(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("This command only works in a server.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.followup.send("Paused", ephemeral=True)
    else:
        await interaction.followup.send("Nothing is playing", ephemeral=True)


async def _resume_core(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("This command only works in a server.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.followup.send("Resumed", ephemeral=True)
    else:
        await interaction.followup.send("Nothing to resume", ephemeral=True)


async def _stop_core(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("This command only works in a server.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.followup.send("Disconnected", ephemeral=True)
    else:
        await interaction.followup.send("Not connected", ephemeral=True)


async def _skip_core(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild is None:
        await interaction.followup.send("This command only works in a server.", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.followup.send("Skipped", ephemeral=True)
    else:
        await interaction.followup.send("Nothing to skip", ephemeral=True)


# -------- SLASH COMMANDS ONLY --------

@bot.tree.command(name="play", description="Play audio from a URL")
@app_commands.describe(url="Audio URL or search")
async def play_slash(interaction: discord.Interaction, url: str):
    await _play_core(interaction, url)


@bot.tree.command(name="pause", description="Pause the current track")
async def pause_slash(interaction: discord.Interaction):
    await _pause_core(interaction)


@bot.tree.command(name="resume", description="Resume the current track")
async def resume_slash(interaction: discord.Interaction):
    await _resume_core(interaction)


@bot.tree.command(name="stop", description="Stop playback and disconnect")
async def stop_slash(interaction: discord.Interaction):
    await _stop_core(interaction)


@bot.tree.command(name="skip", description="Skip the current track")
async def skip_slash(interaction: discord.Interaction):
    await _skip_core(interaction)


@bot.tree.command(name="message", description="Owner only: Send a message to the channel")
@app_commands.describe(message="Message to send")
async def message_slash(interaction: discord.Interaction, message: str):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("No eres mi jefe puñetas", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if interaction.channel is None:
        await interaction.followup.send("No channel available here.", ephemeral=True)
        return

    await interaction.channel.send(message)
    await interaction.followup.send("Enviado.", ephemeral=True)


# Logging config (discord.py internal)
logger = logging.getLogger("discord")
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
logger.propagate = False
logger.addHandler(handler)

app_logger = logging.getLogger(__name__)
if not app_logger.handlers:
    app_logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"))
    app_logger.addHandler(console)


if __name__ == "__main__":
    if callable(keep_alive):
        keep_alive()

    bot.run(TOKEN)