import discord
from discord import app_commands
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import yt_dlp
import asyncio
from keep_alive import keep_alive

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='/', intents=intents)


@bot.event
async def on_ready():
    logging.getLogger(__name__).info("%s has connected to Discord!", bot.user)
    try:
        await bot.tree.sync()
    except Exception as exc:
        logging.getLogger(__name__).exception("Slash command sync failed: %s", exc)


YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'nocheckcertificate': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'youtube_include_dash_manifest': False,
    'cachedir': False,
    'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
}

if os.path.exists('cookies.txt'):
    YDL_OPTIONS['cookiefile'] = 'cookies.txt'

FFMPEG_PATH = r"C:\ffmpeg\bin\ffmpeg.exe"

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_at_eof 1 -reconnect_on_network_error 1 '
                      '-reconnect_on_http_error 4xx,5xx -reconnect_delay_max 5 '
                      '-probesize 10M -analyzeduration 10M '
                      '-user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"',
    'options': '-vn -buffer_size 10M'
}

_queues: dict[int, asyncio.Queue] = {}
_players: dict[int, asyncio.Task] = {}


def _log_play_error(error: Exception | None) -> None:
    if error:
        logging.getLogger(__name__).error("Player error: %s", error)

def _get_author(ctx):
    return ctx.user if isinstance(ctx, discord.Interaction) else ctx.author


def _get_voice_client(ctx):
    return ctx.guild.voice_client if isinstance(ctx, discord.Interaction) else ctx.voice_client


async def _send(ctx, message: str = None, embed: discord.Embed = None, ephemeral: bool = False) -> discord.Message | discord.InteractionMessage | None:
    if isinstance(ctx, discord.Interaction):
        if ctx.response.is_done():
            return await ctx.followup.send(content=message, embed=embed, ephemeral=ephemeral)
        else:
            await ctx.response.send_message(content=message, embed=embed, ephemeral=ephemeral)
            return await ctx.original_response()
    else:
        return await ctx.send(content=message, embed=embed)

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
                # Try with a different User-Agent or without some options if it fails
                # This is a simple fallback mechanism
                temp_options = YDL_OPTIONS.copy()
                temp_options['user_agent'] = 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'
                # Fallback to standard clients if custom one fails
                if 'extractor_args' in temp_options:
                    del temp_options['extractor_args']
                with yt_dlp.YoutubeDL(temp_options) as ydl2:
                    info = ydl2.extract_info(url, download=False)

            if 'entries' in info:
                # If it's a playlist, take the first entry
                info = info['entries'][0]

            return {
                "url": info["url"],
                "title": info.get("title", "Unknown"),
                "headers": info.get("http_headers", {}),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url")
            }

    return await loop.run_in_executor(None, _extract)


async def _player_loop(guild: discord.Guild) -> None:
    queue = _get_queue(guild.id)
    while True:
        info, ctx = await queue.get()

        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            queue.task_done()
            continue

        audio_url = info["url"]
        headers = info["headers"]
        title = info["title"]

        before_options = FFMPEG_OPTIONS["before_options"]
        if headers:
            # Filter headers that might cause issues with FFmpeg or are redundant
            # FFmpeg's -user_agent already handles User-Agent
            filtered_headers = {k: v for k, v in headers.items() if k.lower() not in ['user-agent', 'host']}
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
            after=_log_play_error
        )

        # Create Now Playing Embed
        embed = discord.Embed(title="Now Playing", description=f"[{title}]({info.get('webpage_url')})", color=discord.Color.blue())
        if info.get("thumbnail"):
            embed.set_thumbnail(url=info["thumbnail"])
        
        duration = info.get("duration")
        if duration:
            minutes, seconds = divmod(duration, 60)
            embed.add_field(name="Duration", value=f"{minutes:02d}:{seconds:02d}", inline=True)
        
        embed.add_field(name="Requested by", value=_get_author(ctx).mention, inline=True)

        await _send(ctx, embed=embed)
        
        while voice_client.is_playing() or voice_client.is_paused():
            await asyncio.sleep(0.5)

        queue.task_done()


async def _play_core(ctx, url: str) -> None:
    author = _get_author(ctx)
    if not author.voice:
        await _send(ctx, "You are not in a voice channel.", ephemeral=True)
        return

    channel = author.voice.channel
    voice_client = _get_voice_client(ctx)
    if not voice_client:
        await channel.connect()

    voice_client = _get_voice_client(ctx)
    if not voice_client or not voice_client.is_connected():
        await _send(ctx, "Error connecting to the voice channel.", ephemeral=True)
        return

    status_msg = await _send(ctx, "🔍 Searching...", ephemeral=True)

    try:
        info = await _extract_info(url)
    except Exception as exc:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        await _send(ctx, "Error retrieving audio information. Please try another URL.", ephemeral=True)
        logging.getLogger(__name__).exception("yt-dlp extract failed: %s", exc)
        return

    queue = _get_queue(voice_client.guild.id)
    was_idle = (not voice_client.is_playing()) and queue.empty()
    await queue.put((info, ctx))
    
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if not was_idle:
        embed = discord.Embed(title="Added to queue", description=f"[{info['title']}]({info.get('webpage_url')})", color=discord.Color.green())
        if info.get("thumbnail"):
            embed.set_thumbnail(url=info["thumbnail"])
        await _send(ctx, embed=embed)

    if voice_client.guild.id not in _players or _players[voice_client.guild.id].done():
        _players[voice_client.guild.id] = asyncio.create_task(_player_loop(voice_client.guild))

@bot.command(name='play', help='plays url music')
async def play(ctx, url: str):
    await _play_core(ctx, url)


@app_commands.command(name="play", description="Play audio from a URL")
@app_commands.describe(url="Audio URL")
async def play_slash(interaction: discord.Interaction, url: str) -> None:
    await interaction.response.defer(ephemeral=True)
    await _play_core(interaction, url)


async def _pause_core(ctx) -> None:
    voice_client = _get_voice_client(ctx)
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await _send(ctx, 'Paused', ephemeral=True)
    else:
        await _send(ctx, 'Nothing is playing', ephemeral=True)


@bot.command(name='pause', help='Pause the music')
async def pause(ctx):
    await _pause_core(ctx)


@app_commands.command(name="pause", description="Pause the current track")
async def pause_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await _pause_core(interaction)


async def _resume_core(ctx) -> None:
    voice_client = _get_voice_client(ctx)
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await _send(ctx, 'Resumed', ephemeral=True)


@bot.command(name='resume', help='Resume the music')
async def resume(ctx):
    await _resume_core(ctx)


@app_commands.command(name="resume", description="Resume the current track")
async def resume_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await _resume_core(interaction)


async def _stop_core(ctx) -> None:
    voice_client = _get_voice_client(ctx)
    if voice_client:
        await voice_client.disconnect()
        await _send(ctx, 'Disconnected', ephemeral=True)


async def _skip_core(ctx) -> None:
    voice_client = _get_voice_client(ctx)
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await _send(ctx, 'Skipped', ephemeral=True)
    else:
        await _send(ctx, 'Nothing to skip', ephemeral=True)


@bot.command(name='stop', help='Stop and disconnect the bot')
async def stop(ctx):
    await _stop_core(ctx)


@app_commands.command(name="stop", description="Stop playback and disconnect")
async def stop_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await _stop_core(interaction)


@bot.command(name='skip', help='Skip the current track')
async def skip(ctx):
    await _skip_core(ctx)


@app_commands.command(name="skip", description="Skip the current track")
async def skip_slash(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await _skip_core(interaction)


bot.tree.add_command(play_slash)
bot.tree.add_command(pause_slash)
bot.tree.add_command(resume_slash)
bot.tree.add_command(stop_slash)
bot.tree.add_command(skip_slash)

logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
logger.handlers.clear()
logger.propagate = False
logger.addHandler(handler)

app_logger = logging.getLogger(__name__)
if not app_logger.handlers:
    app_logger.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'))
    app_logger.addHandler(console)

if __name__ == "__main__":
    keep_alive()  # ← Start Flask server

    TOKEN = os.getenv('DISCORD_TOKEN')
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN not found")
        exit(1)

    bot.run(TOKEN)
