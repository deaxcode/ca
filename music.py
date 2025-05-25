import asyncio
import concurrent.futures
import datetime
import json
import logging
import math
import os
import random
import traceback
from datetime import timedelta
import concurrent
import re
import sys
import time

import discord
import lyricsgenius
from discord.ext import commands, tasks
from spotdl import Spotdl
from spotdl.types.album import Album
from spotdl.types.playlist import Playlist
from spotdl.types.song import Song

from musicbot_objects.item import Item
from musicbot_objects.queue import Queue
from musicbot_objects.state import GuildState
from musicbot_source.filesource import DiscordFileSource
from musicbot_source.niconico import NicoNicoSource
from musicbot_source.source import YTDLSource, isPlayList
from musicbot_utils.func import clamp, formatTime
from musicbot_utils.search import searchNicoNico, searchYoutube
from musicbot_utils.translations import Localization

# メインモジュールからFFMPEG_PATHを取得
try:
    from musicbot_main import __FFMPEG_PATH as FFMPEG_PATH
except ImportError:
    # FFMPEGのパスを決定
    if sys.platform == "win32":
        ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg", "bin", "ffmpeg.exe")
    else:
        ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg", "bin", "ffmpeg")
    
    if os.path.exists(ffmpeg_path):
        FFMPEG_PATH = ffmpeg_path
    else:
        import shutil
        FFMPEG_PATH = shutil.which("ffmpeg")
        if not FFMPEG_PATH:
            print("警告: FFMPEGが見つかりません。音楽の再生には必要です。")
            FFMPEG_PATH = "ffmpeg"  # デフォルト値

# 設定ファイルを読み込む
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
except Exception as e:
    print(f"設定ファイルの読み込みに失敗しました: {e}")
    config = {"spotify": {"client_id": "", "client_secret": ""}, "genius_token": ""}

def createView(isPaused: bool, isLooping: bool, isShuffle: bool, hasBassBoost: bool = False):
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, emoji="⏪", custom_id="reverse", row=0
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple,
            emoji="▶" if isPaused else "⏸",
            custom_id="resume" if isPaused else "pause",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, emoji="⏩", custom_id="forward", row=0
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, label="+", custom_id="volumeUp", row=0
        )
    )
    view.add_item(
        discord.ui.Button(
            style=(
                discord.ButtonStyle.blurple
                if not isLooping
                else discord.ButtonStyle.danger
            ),
            emoji="🔄",
            custom_id="loop",
            row=0,
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, emoji="⏮", custom_id="prev", row=1
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, emoji="⏹", custom_id="stop", row=1
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, emoji="⏭", custom_id="next", row=1
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple, label="-", custom_id="volumeDown", row=1
        )
    )
    view.add_item(
        discord.ui.Button(
            style=(
                discord.ButtonStyle.blurple
                if not isShuffle
                else discord.ButtonStyle.danger
            ),
            emoji="🔀",
            custom_id="shuffle",
            row=1,
        )
    )
    view.add_item(
        discord.ui.Button(
            style=(
                discord.ButtonStyle.blurple
                if not hasBassBoost
                else discord.ButtonStyle.danger
            ),
            emoji="🔊",
            custom_id="bassboost",
            row=2,
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.blurple,
            emoji="📝",
            custom_id="lyrics",
            row=2,
        )
    )
    return view


class MusicCog(commands.Cog):
    __slots__ = (
        "bot",
        "queue",
        "playing",
        "alarm",
        "presenceCount",
        "spotify",
        "genius",
        "bass_boost",
        "idle_timeout",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guildStates: dict[int, GuildState] = {}
        self.presenceCount = 0
        
        # プログレスバー用絵文字のデフォルト値を設定
        self.bar = "▬"
        self.circle = "⚪" 
        self.graybar = "─"
        
        # Spotifyクライアントの初期化
        spotify_client_id = config["spotify"]["client_id"]
        spotify_client_secret = config["spotify"]["client_secret"]
        
        if spotify_client_id and spotify_client_secret:
            try:
                self.spotify = Spotdl(
                    client_id=spotify_client_id,
                    client_secret=spotify_client_secret,
                    threads=2,  # スレッド数を制限して負荷を減らす
                    bitrate=128,  # ビットレートを下げて負荷を減らす
                )
                # ダウンローダーのスレッド数を制限
                if hasattr(self.spotify, 'downloader') and hasattr(self.spotify.downloader, 'settings'):
                    self.spotify.downloader.settings['threads'] = 2
            except Exception as e:
                print(f"警告: Spotify APIの初期化に失敗しました: {e}")
                # APIキーが設定されていない場合は空のモックオブジェクトを作成
                self.spotify = type('DummySpotdl', (), {
                    'downloader': type('DummyDownloader', (), {'settings': {'threads': 1}})
                })()
        else:
            # APIキーが設定されていない場合は空のモックオブジェクトを作成
            self.spotify = type('DummySpotdl', (), {
                'downloader': type('DummyDownloader', (), {'settings': {'threads': 1}})
            })()
            print("警告: Spotify APIキーが設定されていないため、Spotify機能は無効です")
        
        self.isFirstReady: bool = True
        
        # Geniusクライアントの初期化
        genius_token = config.get("genius_token", "")
        if genius_token:
            try:
                self.genius = lyricsgenius.Genius(genius_token)
            except Exception as e:
                print(f"警告: Genius APIの初期化に失敗しました: {e}")
                self.genius = None
        else:
            print("警告: Genius APIトークンが設定されていないため、歌詞表示機能は無効です")
            self.genius = None
            
        # アイドルタイマーの設定（3分=180秒）
        self.idle_timeout = 180
        # 自動切断タスクを開始
        self.auto_disconnect.start()

    @commands.Cog.listener()
    async def on_ready(self):
        if self.isFirstReady:
            for guild in self.bot.guilds:
                self.guildStates[guild.id] = GuildState()
            self.presenceLoop.start()
            self.isFirstReady = False
        try:
            self.bar = str(
                discord.utils.get(await self.bot.fetch_application_emojis(), name="bar")
            )
            self.circle = str(
                discord.utils.get(await self.bot.fetch_application_emojis(), name="circle")
            )
            self.graybar = str(
                discord.utils.get(await self.bot.fetch_application_emojis(), name="graybar")
            )
            
            # 絵文字が見つからない場合はデフォルト値を設定
            if self.bar == "None" or not self.bar:
                self.bar = "▬"
            if self.circle == "None" or not self.circle:
                self.circle = "⚪"
            if self.graybar == "None" or not self.graybar:
                self.graybar = "─"
        except Exception as e:
            print(f"プログレスバーの絵文字取得に失敗しました: {e}")
            # デフォルト値を設定
            self.bar = "▬"
            self.circle = "⚪"
            self.graybar = "─"

    @tasks.loop(seconds=20)
    async def presenceLoop(self):
        if self.presenceCount == 0:
            await self.bot.change_presence(
                activity=discord.Activity(
                    name=f"{len(self.bot.voice_clients)} / {len(self.bot.guilds)} サーバー",
                    type=discord.ActivityType.competing,
                )
            )
            self.presenceCount = 1
        elif self.presenceCount == 1:
            await self.bot.change_presence(activity=discord.Game("/help"))
            self.presenceCount = 2
        elif self.presenceCount == 2:
            await self.bot.change_presence(
                activity=discord.Game("Powered by deax")
            )
            self.presenceCount = 0

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        self.guildStates[guild.id] = GuildState()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await asyncio.sleep(2)
        del self.guildStates[guild.id]

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        try:
            if interaction.data["component_type"] == 2:
                await self.onButtonClick(interaction)
            elif interaction.data["component_type"] == 3:
                pass
        except KeyError:
            pass

    def seekMusic(
        self, source: YTDLSource | NicoNicoSource | DiscordFileSource, seconds: float, bass_boost: bool = False
    ) -> YTDLSource | NicoNicoSource | DiscordFileSource:
        options = self.get_audio_options(source, seconds, bass_boost)
        
        if isinstance(source, NicoNicoSource):
            options["before_options"] = (
                f"-headers 'cookie: {'; '.join(f'{k}={v}' for k, v in source.client.cookies.items())}' {options['before_options']}"
            )
            return NicoNicoSource(
                discord.FFmpegPCMAudio(source.hslContentUrl, executable=FFMPEG_PATH, **options),
                info=source.info,
                hslContentUrl=source.hslContentUrl,
                watchid=source.watchid,
                trackid=source.trackid,
                outputs=source.outputs,
                nicosid=source.nicosid,
                niconico=source.niconico,
                volume=source.volume,
                progress=seconds / 0.02,
                user=source.user,
            )
        elif isinstance(source, DiscordFileSource):
            return DiscordFileSource(
                discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                info=source.info,
                volume=source.volume,
                progress=seconds / 0.02,
                user=source.user,
            )
        else:
            return YTDLSource(
                discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                info=source.info,
                volume=source.volume,
                progress=seconds / 0.02,
                user=source.user,
                locale=source.locale if hasattr(source, 'locale') else discord.Locale.japanese,
            )
            
    def get_audio_options(self, source, position=0, bass_boost=False, quality="high"):
        """音声オプションを生成します。

        Args:
            source: 音声ソース
            position (int, optional): 開始位置（秒）. デフォルトは0.
            bass_boost (bool, optional): ベースブースト. デフォルトはFalse.
            quality (str, optional): 音質. デフォルトは"high".

        Returns:
            dict: FFmpegのオプション
        """
        options = {
            "before_options": f"-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -ss {position} -fflags +discardcorrupt+genpts",
            "options": f"-vn -ac 2 -bufsize 128k -analyzeduration 10000000 -probesize 5000000 -threads 0",
        }
        
        # ベースブーストフィルターの追加
        if bass_boost:
            options["options"] += " -af bass=g=10,dynaudnorm=f=150:g=15:n=0"
            
        # 音質設定
        if quality == "ultra":
            options["options"] += " -acodec libopus -b:a 256k"
        elif quality == "high":
            options["options"] += " -acodec libopus -b:a 192k"
        elif quality == "medium":
            options["options"] += " -acodec libopus -b:a 128k"
        else:  # low
            options["options"] += " -acodec libopus -b:a 96k"
            
        return options

    async def queuePagenation(
        self, interaction: discord.Interaction, page: int = None, *, edit: bool = False
    ):
        await interaction.response.defer()
        queue: Queue = self.guildStates[interaction.guild.id].queue
        pageSize = 10
        index = queue.index
        if page is None:
            page = (index // pageSize) + 1
        songList: tuple[Item] = queue.pagenation(page, pageSize=pageSize)
        songs = ""
        startIndex = (page - 1) * pageSize

        for i, song in enumerate(songList):
            if startIndex + i == index - 1:
                songs += f"{song.name} by {song.user.mention} (現在再生中)\n"
            else:
                songs += f"{song.name} by {song.user.mention}\n"

        view = (
            discord.ui.View(timeout=None)
            .add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji="⏪",
                    custom_id=f"queuePagenation,{page-1}",
                    row=0,
                    disabled=(page <= 1),
                )
            )
            .add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.gray,
                    emoji="🔄",
                    label=f"ページ {page} / {(queue.asize() // pageSize) + 1}",
                    custom_id=f"queuePagenation,{page}",
                    row=0,
                )
            )
            .add_item(
                discord.ui.Button(
                    style=discord.ButtonStyle.blurple,
                    emoji="⏩",
                    custom_id=f"queuePagenation,{page+1}",
                    row=0,
                    disabled=((queue.asize() // pageSize) + 1 == page),
                )
            )
        )
        embed = discord.Embed(title=f"キュー", description=songs)
        if edit:
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view)

    async def onButtonClick(self, interaction: discord.Interaction):
        customField = interaction.data["custom_id"].split(",")
        match (customField[0]):
            case "prev":
                if not interaction.guild.voice_client:
                    await interaction.response.send_message(
                        "現在曲を再生していません。", ephemeral=True
                    )
                    return
                await interaction.response.defer(ephemeral=True)
                self.guildStates[interaction.guild.id].queue.prev()
                interaction.guild.voice_client.stop()
            case "next":
                if not interaction.guild.voice_client:
                    await interaction.response.send_message(
                        "現在曲を再生していません。", ephemeral=True
                    )
                    return
                await interaction.response.defer(ephemeral=True)
                self.guildStates[interaction.guild.id].playing = False
                interaction.guild.voice_client.stop()
            case "stop":
                if not interaction.guild.voice_client:
                    await interaction.response.send_message(
                        "現在曲を再生していません。", ephemeral=True
                    )
                    return
                await interaction.response.defer()
                await interaction.guild.voice_client.disconnect()
                self.guildStates[interaction.guild.id].playing = False
            case "resume":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                interaction.guild.voice_client.resume()
                embed = interaction.message.embeds[0]
                await interaction.edit_original_response(
                    embed=embed,
                    view=createView(
                        isPaused=False,
                        isLooping=self.guildStates[interaction.guild.id].loop,
                        isShuffle=self.guildStates[interaction.guild.id].shuffle,
                        hasBassBoost=self.guildStates[interaction.guild.id].bass_boost,
                    ),
                )
            case "pause":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                interaction.guild.voice_client.pause()
                embed = interaction.message.embeds[0]
                await interaction.edit_original_response(
                    embed=embed,
                    view=createView(
                        isPaused=True,
                        isLooping=self.guildStates[interaction.guild.id].loop,
                        isShuffle=self.guildStates[interaction.guild.id].shuffle,
                        hasBassBoost=self.guildStates[interaction.guild.id].bass_boost,
                    ),
                )
            case "reverse":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                source: YTDLSource | NicoNicoSource = (
                    interaction.guild.voice_client.source
                )
                interaction.guild.voice_client.source = self.seekMusic(
                    source, source.progress - 10, self.guildStates[interaction.guild.id].bass_boost
                )
            case "forward":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                source: YTDLSource | NicoNicoSource = (
                    interaction.guild.voice_client.source
                )
                interaction.guild.voice_client.source = self.seekMusic(
                    source, source.progress + 10, self.guildStates[interaction.guild.id].bass_boost
                )
            case "volumeUp":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                if interaction.guild.voice_client.source.volume < 2.0:
                    interaction.guild.voice_client.source.volume = (
                        math.floor(
                            (interaction.guild.voice_client.source.volume + 0.1) * 100
                        )
                        / 100
                    )
                    embed = interaction.message.embeds[0]
                    await interaction.edit_original_response(
                        embed=embed,
                        view=createView(
                            isPaused=interaction.guild.voice_client.is_paused(),
                            isLooping=self.guildStates[interaction.guild.id].loop,
                            isShuffle=self.guildStates[interaction.guild.id].shuffle,
                            hasBassBoost=self.guildStates[interaction.guild.id].bass_boost,
                        ),
                    )
            case "volumeDown":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                if interaction.guild.voice_client.source.volume > 0.0:
                    interaction.guild.voice_client.source.volume = (
                        math.floor(
                            (interaction.guild.voice_client.source.volume - 0.1) * 100
                        )
                        / 100
                    )
                    embed = interaction.message.embeds[0]
                    await interaction.edit_original_response(
                        embed=embed,
                        view=createView(
                            isPaused=interaction.guild.voice_client.is_paused(),
                            isLooping=self.guildStates[interaction.guild.id].loop,
                            isShuffle=self.guildStates[interaction.guild.id].shuffle,
                            hasBassBoost=self.guildStates[interaction.guild.id].bass_boost,
                        ),
                    )
            case "loop":
                await interaction.response.defer(ephemeral=True)
                self.guildStates[interaction.guild.id].loop = not self.guildStates[
                    interaction.guild.id
                ].loop
                embed = interaction.message.embeds[0]
                await interaction.edit_original_response(
                    embed=embed,
                    view=createView(
                        isPaused=False,
                        isLooping=self.guildStates[interaction.guild.id].loop,
                        isShuffle=self.guildStates[interaction.guild.id].shuffle,
                        hasBassBoost=self.guildStates[interaction.guild.id].bass_boost,
                    ),
                )
            case "shuffle":
                await interaction.response.defer(ephemeral=True)
                self.guildStates[interaction.guild.id].shuffle = not self.guildStates[
                    interaction.guild.id
                ].shuffle
                embed = interaction.message.embeds[0]
                await interaction.edit_original_response(
                    embed=embed,
                    view=createView(
                        isPaused=False,
                        isLooping=self.guildStates[interaction.guild.id].loop,
                        isShuffle=self.guildStates[interaction.guild.id].shuffle,
                        hasBassBoost=self.guildStates[interaction.guild.id].bass_boost,
                    ),
                )
            case "queuePagenation":
                if not interaction.guild.voice_client or (
                    self.guildStates[interaction.guild.id].queue.qsize() <= 0
                ):
                    await interaction.response.send_message(
                        "現在曲を再生していません。", ephemeral=True
                    )
                    return
                await self.queuePagenation(interaction, int(customField[1]), edit=True)
            case "bassboost":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                await interaction.response.defer(ephemeral=True)
                
                guild_state = self.guildStates[interaction.guild.id]
                guild_state.bass_boost = not guild_state.bass_boost
                
                source = interaction.guild.voice_client.source
                current_position = source.progress
                
                # FFmpegオプションにベースブースト設定を追加
                options = {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    "options": f"-vn -ss {formatTime(current_position)} -bufsize 64k -analyzeduration 2147483647 -probesize 2147483647 -ac 2",
                }
                
                if guild_state.bass_boost:
                    options["options"] += " -af \"equalizer=f=40:width_type=h:width=50:g=10,equalizer=f=80:width_type=h:width=50:g=6,equalizer=f=150:width_type=h:width=50:g=3\""
                
                # 音源タイプに応じた処理
                if isinstance(source, NicoNicoSource):
                    options["before_options"] = (
                        f"-headers 'cookie: {'; '.join(f'{k}={v}' for k, v in source.client.cookies.items())}' {options['before_options']}"
                    )
                    interaction.guild.voice_client.source = NicoNicoSource(
                        discord.FFmpegPCMAudio(source.hslContentUrl, executable=FFMPEG_PATH, **options),
                        info=source.info,
                        hslContentUrl=source.hslContentUrl,
                        watchid=source.watchid,
                        trackid=source.trackid,
                        outputs=source.outputs,
                        nicosid=source.nicosid,
                        niconico=source.niconico,
                        volume=source.volume,
                        progress=current_position / 0.02,
                        user=source.user,
                    )
                elif isinstance(source, DiscordFileSource):
                    interaction.guild.voice_client.source = DiscordFileSource(
                        discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                        info=source.info,
                        volume=source.volume,
                        progress=current_position / 0.02,
                        user=source.user,
                    )
                else:
                    interaction.guild.voice_client.source = YTDLSource(
                        discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                        info=source.info,
                        volume=source.volume,
                        progress=current_position / 0.02,
                        user=source.user,
                        locale=source.locale if hasattr(source, 'locale') else discord.Locale.japanese,
                    )
                
                embed = discord.Embed(
                    title="成功",
                    description=f"ベースブーストを{'有効' if guild_state.bass_boost else '無効'}にしました。",
                    color=discord.Color.green()
                )
                await interaction.followup.send(embed=embed)
            
            case "lyrics":
                if not interaction.guild.voice_client:
                    embed = discord.Embed(
                        title="音楽を再生していません。", colour=discord.Colour.red()
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                
                # Genius APIが無効な場合
                if self.genius is None:
                    await interaction.response.send_message(
                        "歌詞表示機能は現在無効です。config.jsonにGenius APIトークンを設定してください。", 
                        ephemeral=True
                    )
                    return
                
                await interaction.response.defer()
                
                source = interaction.guild.voice_client.source
                title = source.info.title
                
                # クリーンなタイトルを作成（括弧や特殊文字を削除）
                clean_title = re.sub(r'\([^)]*\)|\[[^\]]*\]|ft\..*|feat\..*|-.*', '', title).strip()
                
                try:
                    song = await asyncio.to_thread(self.genius.search_song, clean_title)
                    if song:
                        lyrics = song.lyrics
                        # 歌詞が長すぎる場合は分割
                        if len(lyrics) > 4000:
                            parts = [lyrics[i:i+4000] for i in range(0, len(lyrics), 4000)]
                            for i, part in enumerate(parts):
                                embed = discord.Embed(
                                    title=f"{title}の歌詞 - パート{i+1}/{len(parts)}",
                                    description=part,
                                    color=discord.Colour.blue()
                                )
                                if i == 0:
                                    await interaction.followup.send(embed=embed)
                                else:
                                    await interaction.channel.send(embed=embed)
                        else:
                            embed = discord.Embed(
                                title=f"{title}の歌詞",
                                description=lyrics,
                                color=discord.Colour.blue()
                            )
                            await interaction.followup.send(embed=embed)
                    else:
                        await interaction.followup.send(f"「{clean_title}」の歌詞が見つかりませんでした。")
                except Exception as e:
                    await interaction.followup.send(f"歌詞の取得中にエラーが発生しました: {str(e)}")

    def setToNotPlaying(self, guildId: int):
        """再生していない状態に設定します。"""
        if guildId in self.guildStates:
            self.guildStates[guildId].playing = False
            try:
                # メインループを使用
                loop = self.bot.loop
                
                # スレッドセーフに実行するための関数
                async def update_activity():
                    self.guildStates[guildId].last_activity = loop.time()
                
                # スレッド間で安全に実行
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(update_activity(), loop)
                    try:
                        # タイムアウトを設定して待機
                        future.result(timeout=1)
                    except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
                        print(f"Warning: Failed to update activity timer for guild {guildId} (timeout)")
                    except Exception as e:
                        print(f"Warning: Failed to update activity timer for guild {guildId}: {e}")
                else:
                    # ループが実行されていない場合はそのまま設定（主に起動直後など）
                    self.guildStates[guildId].last_activity = time.time()
            except Exception as e:
                print(f"Error updating activity timer: {e}")
                # 最終手段としてタイムスタンプを直接設定
                self.guildStates[guildId].last_activity = time.time()

    def safe_after_callback(self, error=None):
        """再生終了後のコールバック関数をスレッドセーフに処理する"""
        guild_id = None
        
        # エラー発生時のログ記録
        if error:
            print(f"再生エラーが発生しました: {error}")
            
            try:
                # スレッドセーフに実行するためにrun_coroutine_threadsafeを使用
                async def report_error():
                    try:
                        for guild in self.bot.guilds:
                            if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                                print(f"Guild {guild.id} is still playing, skipping error report")
                                continue
                                
                            # 該当するギルドを見つけたとみなす（近似）
                            ctx = None
                            for channel in guild.text_channels:
                                if channel.permissions_for(guild.me).send_messages:
                                    try:
                                        if channel.last_message_id:
                                            ctx = await self.bot.get_context(await channel.fetch_message(channel.last_message_id))
                                            if ctx:
                                                break
                                    except:
                                        continue
                            
                            if ctx:
                                embed = discord.Embed(
                                    title="再生エラー",
                                    description=f"音楽の再生中にエラーが発生しました。再接続してください。\n`{str(error)}`",
                                    color=discord.Color.red()
                                )
                                await ctx.send(embed=embed)
                    except Exception as e:
                        print(f"エラー通知の送信中にエラーが発生: {e}")
                
                asyncio.run_coroutine_threadsafe(report_error(), self.bot.loop)
            except Exception as e:
                print(f"エラー処理中にエラーが発生: {e}")
        
        # 各ギルドの状態をチェック
        for guild_id in list(self.guildStates.keys()):
            guild = self.bot.get_guild(guild_id)
            if not guild or not guild.voice_client or not guild.voice_client.is_playing():
                # 再生していないギルドを見つけたので、状態をリセット
                try:
                    self.guildStates[guild_id].playing = False
                    self.guildStates[guild_id].last_activity = time.time()
                except Exception as e:
                    print(f"ギルド {guild_id} の状態リセット中にエラー: {e}")
        
        # 次の曲を再生する（可能であれば）
        try:
            # キューに曲がある場合は次を再生
            for guild_id in list(self.guildStates.keys()):
                if guild_id in self.guildStates and not self.guildStates[guild_id].queue.empty():
                    guild = self.bot.get_guild(guild_id)
                    if guild and guild.voice_client and not guild.voice_client.is_playing():
                        # テキストチャンネルを探す
                        channel = None
                        for text_channel in guild.text_channels:
                            try:
                                if text_channel.permissions_for(guild.me).send_messages:
                                    channel = text_channel
                                    break
                            except:
                                continue
                        
                        if channel:
                            # playNextを非同期で実行
                            async def play_next_song():
                                try:
                                    await self.playNext(guild, channel)
                                except Exception as e:
                                    print(f"次の曲の再生中にエラー: {e}")
                            
                            future = asyncio.run_coroutine_threadsafe(play_next_song(), self.bot.loop)
                            try:
                                future.result(timeout=30)  # 最大30秒待機
                            except concurrent.futures.TimeoutError:
                                print(f"次の曲の再生がタイムアウトしました: Guild {guild_id}")
                            except Exception as e:
                                print(f"次の曲の再生待機中にエラー: {e}")
        except Exception as e:
            print(f"再生後の処理中にエラー: {e}")

    def embedPanel(
        self,
        voiceClient: discord.VoiceClient,
        *,
        source: YTDLSource | NicoNicoSource = None,
        finished: bool = False,
    ):
        if source is None:
            if voiceClient.source is None:
                return None
            source: YTDLSource | NicoNicoSource | DiscordFileSource = voiceClient.source
        embed = discord.Embed(
            title=source.info.title,
            url=source.info.webpage_url,
        ).set_image(url=source.info.thumbnail)

        if finished:
            embed.colour = discord.Colour.greyple()
            embed.set_author(name="再生終了")
        elif voiceClient.is_playing() or voiceClient.is_paused():
            # プログレスバーに使用する絵文字が設定されていない場合のチェック
            bar = self.bar if self.bar and self.bar != "None" else "▬"
            circle = self.circle if self.circle and self.circle != "None" else "⚪"
            graybar = self.graybar if self.graybar and self.graybar != "None" else "─"

            percentage = source.progress / source.info.duration
            barLength = 14
            filledLength = int(barLength * percentage)
            progressBar = (
                bar * filledLength
                + circle
                + graybar * (barLength - filledLength - 1)
            )

            percentage = source.volume / 2.0
            barLength = 14
            filledLength = int(barLength * percentage)
            volumeProgressBar = (
                bar * filledLength
                + circle
                + graybar * (barLength - filledLength - 1)
            )

            embed.colour = discord.Colour.purple()
            if voiceClient.is_paused():
                embed.set_author(name="一時停止中")
            else:
                embed.set_author(name="再生中")
            embed.add_field(
                name="再生時間",
                value=f"{progressBar}\n`{formatTime(source.progress)} / {formatTime(source.info.duration)}`",
                inline=False,
            ).add_field(
                name="リクエストしたユーザー",
                value=f"{source.user.mention}",
                inline=False,
            ).add_field(
                name="ボリューム",
                value=f"{volumeProgressBar}\n`{source.volume} / 2.0`",
                inline=False,
            )
        else:
            embed.colour = discord.Colour.greyple()
            embed.set_author(name="再生準備中")
        return embed

    async def getSourceFromQueue(self, queue: Queue):
        info: Item = queue.get()
        if info.attachment is not None:
            return await DiscordFileSource.from_attachment(
                info.attachment, info.volume, info.user
            )
        elif ("nicovideo.jp" in info.url) or ("nico.ms" in info.url):
            source = await NicoNicoSource.from_url(
                info.url, 
                info.volume, 
                info.user, 
                video_mode=info.video_mode, 
                quality=info.quality
            )
            return source
        else:
            source = await YTDLSource.from_url(
                info.url, 
                info.locale, 
                info.volume, 
                info.user
            )
            # video_modeフラグを設定
            source.video_mode = info.video_mode
            source.quality = info.quality
            return source

    async def newSource(self, source: YTDLSource) -> YTDLSource:
        options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": f"-vn -ss {formatTime(0)} -bufsize 64k -analyzeduration 2147483647 -probesize 2147483647 -ac 2",
        }

        # 元のソースから属性を取得
        video_mode = getattr(source, 'video_mode', False)
        quality = getattr(source, 'quality', 'high')
        
        # 動画モードの場合はオプションを調整
        if video_mode:
            guild_id = source.user.guild.id if source.user and source.user.guild else None
            if guild_id and guild_id in self.guildStates:
                bass_boost = self.guildStates[guild_id].bass_boost
            else:
                bass_boost = False
                
            options = self.get_audio_options(source, 0, 
                                         bass_boost=bass_boost,
                                         quality=quality,
                                         video_mode=True)

        if isinstance(source, DiscordFileSource):
            new_source = DiscordFileSource(
                discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                info=source.info,
                volume=source.volume,
                progress=0,
                user=source.user,
                video_mode=video_mode,
                quality=quality,
            )
            return new_source
        elif isinstance(source, NicoNicoSource):
            new_source = await NicoNicoSource.from_url(
                source.info.webpage_url, source.volume, source.user
            )
            # 属性を設定
            new_source.video_mode = video_mode
            new_source.quality = quality
            return new_source
        else:
            new_source = await YTDLSource.from_url(
                source.info.webpage_url, source.locale, source.volume, source.user
            )
            # 属性を設定
            new_source.video_mode = video_mode
            new_source.quality = quality
            return new_source

    async def playNext(self, guild: discord.Guild, channel: discord.abc.Messageable):
        queue: Queue = self.guildStates[guild.id].queue
        # 新しい曲を再生するときにアクティビティタイマーをリセット
        self.guildStates[guild.id].last_activity = asyncio.get_event_loop().time()
        
        while True:
            if guild.voice_client:
                if queue.empty():
                    break

                if self.guildStates[guild.id].shuffle and not queue.shuffled:
                    queue.shuffle()
                elif queue.shuffled:
                    queue.unshuffle()

                try:
                    source = await self.getSourceFromQueue(queue)
                except:
                    traceback.print_exc()
                    continue

                url = get_url_from_source(source)

                if ("nicovideo.jp" in url) or ("nico.ms" in url):
                    # nicovideo用処理
                    pass

                voiceClient: discord.VoiceClient = guild.voice_client
                
                # 動画モードの場合、self_deafをオフにする
                if getattr(source, 'video_mode', False):
                    try:
                        if voiceClient.is_connected() and voiceClient.is_self_deafened():
                            await voiceClient.edit(self_deaf=False)
                    except Exception as e:
                        print(f"動画モード設定中にエラー: {e}")

                if (voiceClient.channel.type == discord.ChannelType.voice) and (
                    voiceClient.channel.permissions_for(guild.me).value & (1 << 48) != 0
                ):
                    await voiceClient.channel.edit(status=source.info.title)

                # 動画モード用のメッセージ
                if getattr(source, 'video_mode', False):
                    video_embed = discord.Embed(
                        title="🎥 動画モードで再生中",
                        description=f"**{source.info.title}**\n\n画面共有を開始しています。動画が表示されるまでお待ちください。",
                        color=discord.Color.purple()
                    )
                    video_embed.add_field(
                        name="注意", 
                        value="画面共有中はボットが聞こえるようになります。ノイズを避けるため、発言する際はミュートをお願いします。",
                        inline=False
                    )
                    await channel.send(embed=video_embed)

                # 再生状態をセット
                self.guildStates[guild.id].playing = True
                
                # 操作パネルを表示
                view = createView(
                    False,
                    self.guildStates[guild.id].loop,
                    self.guildStates[guild.id].shuffle,
                    self.guildStates[guild.id].bass_boost,
                )
                
                # embedとviewを両方一緒に送信
                message = await channel.send(
                    embed=self.embedPanel(voiceClient, source=source),
                    view=view
                )
                
                # 安全なコールバック関数を使用
                voiceClient.play(source, after=lambda error: self.safe_after_callback(error))

                view.message = message
                    
                return True
            else:
                break
        return False

    def getDownloadUrls(self, songs) -> tuple[
        list[tuple[str, str]],
        list[str],
    ]:
        """
        曲リストからYouTube URLを取得します。

        ### 引数
        - songs: 曲情報のリスト

        ### 戻り値
        - 成功した場合、URLのリストと曲IDのタプル

        ### 注意
        - この関数はマルチスレッドで実行されます。
        """
        
        # Spotifyのさまざまな実装に対応
        print("YouTube URL取得処理開始")
        urls: list[tuple[str, str]] = []
        failedSongs: list[str] = []
        
        # スポティファイ検索機能のチェック
        has_new_api = hasattr(self.spotify, 'search')
        has_old_api = hasattr(self.spotify, 'downloader') and callable(getattr(self.spotify.downloader, 'search', None))
        
        if not (has_new_api or has_old_api):
            print("Spotifyクライアントの検索機能が無効です")
            return [], []
        
        # スレッド数を決定（デフォルト1）
        thread_count = 1
        if hasattr(self.spotify, 'downloader') and hasattr(self.spotify.downloader, 'settings'):
            thread_count = self.spotify.downloader.settings.get("threads", 1)
            
        print(f"マルチスレッド数: {thread_count}")
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            # 各曲の情報からYouTube URLを取得する処理
            def get_youtube_url(song):
                try:
                    # 辞書形式の場合（新しいAPI）
                    if isinstance(song, dict):
                        song_id = song.get('song_id', '') or song.get('url', '')
                        if 'youtube_link' in song:
                            return song['youtube_link'], song_id
                        elif 'url' in song:
                            return song['url'], song_id
                        else:
                            return None, song_id
                    # クラスインスタンスの場合（古いAPI）
                    else:
                        try:
                            # Songオブジェクトの場合
                            song_id = getattr(song, 'song_id', '') or getattr(song, 'url', '')
                            # YouTubeリンクを取得
                            url = None
                            if hasattr(self.spotify, 'downloader') and callable(getattr(self.spotify.downloader, 'search', None)):
                                # 古いAPIを使用
                                try:
                                    url = self.spotify.downloader.search(song)
                                except:
                                    url = None
                            return url, song_id
                        except Exception as e:
                            print(f"Songオブジェクト処理エラー: {e}")
                            return None, "unknown"
                except Exception as e:
                    print(f"YouTube URL取得エラー: {e}")
                    return None, "unknown"
            
            # マルチスレッドで各曲のYouTube URLを取得
            futures = [executor.submit(get_youtube_url, song) for song in songs]
            
            for future in concurrent.futures.as_completed(futures):
                try:
                    url, song_id = future.result()
                    if url:
                        urls.append((url, song_id))
                    else:
                        failedSongs.append(song_id)
                except Exception as e:
                    print(f"曲処理エラー: {e}")
                    failedSongs.append("unknown")

        return urls, failedSongs

    async def putQueue(
        self,
        ctx,
        url: str,
        volume: float,
        quality: str = "high",
    ):
        """URLをキューに追加します。

        Args:
            ctx: コンテキスト
            url (str): URL
            volume (float): 音量
            quality (str, optional): 音質. デフォルトは"high".
        """
        message = None
        
        try:
            if ctx.author.voice is None:
                return await ctx.reply(
                    content="ボイスチャンネルに参加してください！",
                    mention_author=False,
                    delete_after=30
                )
            
            # チェック処理を実行
            check_result = await self.checks(ctx, url=url)
            if not check_result:
                return
                
            # 必要な変数の初期化
            guild = ctx.guild
            guildId = guild.id
            text_channel = ctx.channel
            voiceClient = guild.voice_client
            
            # キューの準備
            if guildId not in self.guildStates:
                self.guildStates[guildId] = GuildState()
                # 音質設定を適用
                self.guildStates[guildId].quality = quality
                
            state = self.guildStates[guildId]
            # stateにはtextChannelを直接使用せず、現在のtext_channelを関数に渡して使用
                
            # ボイスチャンネルに接続
            if voiceClient is None:
                try:
                    voiceClient = await ctx.author.voice.channel.connect()
                except discord.ClientException as e:
                    return await ctx.reply(
                        content=f"ボイスチャンネルへの接続に失敗しました: {e}",
                        mention_author=False,
                    )
                    
            # アイドルタイマーをキャンセル
            if hasattr(self.bot, 'idle_handler'):
                self.bot.idle_handler.cancel_timer(guildId)
            
            # URLがSpotifyであれば特別処理
            if "spotify.com" in url:
                return await self.handle_spotify_playlist(ctx, url, volume, message)
                
            # 進捗メッセージを送信
            if message is None:
                message = await ctx.reply(
                    content="🔍 情報を取得中...",
                    mention_author=False,
                )
            
            # URLがプレイリストかチェック
            from musicbot_source.factory import SourceFactory
            import discord
            # ctx.authorにはlocale属性がないため、デフォルト値を使用
            playlist_info = await SourceFactory.detect_playlist(url, discord.Locale.japanese)
            
            if playlist_info['is_playlist']:
                # プレイリスト処理のためリダイレクト
                await message.delete()
                await self.playPlaylist(ctx, url, volume)
                return
            
            # 音源の作成
            try:
                source = await SourceFactory.create_source(
                    url, discord.Locale.japanese, volume=volume, user=ctx.author,
                    quality=state.quality
                )
            except Exception as e:
                await message.edit(content=f"⚠️ 音源の読み込みに失敗しました: {e}")
                return
                
            # キューに追加
            state.queue.put(Item(url=source, user=ctx.author))
            
            # 追加完了通知
            embed = discord.Embed(
                title="🎵 キューに追加",
                description=f"[{source.info.title}]({source.info.webpage_url})",
                color=discord.Color.green(),
            )
            embed.add_field(name="長さ", value=formatTime(source.info.duration))
            embed.add_field(name="リクエスト", value=ctx.author.mention)
            
            if source.info.thumbnail:
                embed.set_thumbnail(url=source.info.thumbnail)
                
            await message.edit(content="", embed=embed)
            
            # 再生していなければ再生開始
            if not voiceClient.is_playing():
                await self.playNext(guild, text_channel)
                
        except Exception as e:
            if message:
                try:
                    await message.edit(content=f"⚠️ エラーが発生しました: {e}")
                except:
                    pass
            logging.error(f"putQueue中にエラーが発生: {e}")
            logging.error(traceback.format_exc())

    @commands.command(name="playfile", aliases=["pf"], description="Discordのファイルを再生します。")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def playFile(
        self,
        ctx,
        volume: float = 2.0,
    ):
        # 範囲チェック
        if volume < 0.0 or volume > 2.0:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "volume_range"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        if not ctx.message.attachments:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description="ファイルが添付されていません。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        attachment = ctx.message.attachments[0]
        
        if not await self.checks(ctx):
            return
        user = ctx.author
        guild = ctx.guild
        channel = ctx.channel
        
        embed = discord.Embed(
            title=Localization.t(ctx, "loading"),
            description="ファイルを読み込んでいます。",
            color=discord.Color.blue()
        )
        message = await ctx.send(embed=embed)
        
        if not guild.voice_client:
            await user.voice.channel.connect(self_deaf=True)
        queue: Queue = self.guildStates[guild.id].queue
        queue.put(Item(attachment=attachment, volume=volume, user=ctx.author))
        
        embed = discord.Embed(
            title=Localization.t(ctx, "success"),
            description=f"**{attachment.filename}**をキューに追加しました。",
            color=discord.Color.green()
        )
        await message.edit(embed=embed)
        
        if (not self.guildStates[guild.id].playing) and (
            not self.guildStates[guild.id].alarm
        ):
            await self.playNext(guild, channel)

    @commands.group(name="search", description="曲を検索して再生します。", invoke_without_command=True)
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def searchCommandGroup(self, ctx):
        embed = discord.Embed(
            title="検索コマンド",
            description="サブコマンドを指定してください: `youtube` または `niconico`",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)

    @searchCommandGroup.command(name="youtube", description="Youtubeから動画を検索して再生します。")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def searchYoutubeCommand(
        self,
        ctx,
        *,
        keyword: str,
        volume: float = 0.5,
    ):
        # 範囲チェック
        if volume < 0.0 or volume > 2.0:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "volume_range"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title=Localization.t(ctx, "loading"),
            description=Localization.t(ctx, "searching").format(keyword),
            color=discord.Color.blue()
        )
        message = await ctx.send(embed=embed)
        
        videos = await searchYoutube(keyword)
        
        if not videos:
            embed = discord.Embed(
                title="検索結果",
                description=Localization.t(ctx, "search_failed"),
                color=discord.Color.red()
            )
            await message.edit(embed=embed)
            return
        
        # 検索結果の表示
        embed = discord.Embed(
            title="検索結果",
            description=Localization.t(ctx, "select_number"),
            color=discord.Color.blue()
        )
        
        for i, video in enumerate(videos[:5], 1):  # 最大5件まで表示
            embed.add_field(
                name=f"{i}. {video['title']}",
                value=f"{Localization.t(ctx, 'uploaded_by')}: {video['uploader']}",
                inline=False
            )
        
        await message.edit(embed=embed)
        
        # ユーザーの応答を待つ
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()
        
        try:
            response = await self.bot.wait_for('message', timeout=30.0, check=check)
            choice = int(response.content)
            
            if 1 <= choice <= min(5, len(videos)):
                selected = videos[choice-1]
                
                # 選択された動画を再生
                if not await self.checks(ctx, url=selected['url']):
                    return
                    
                user = ctx.author
                guild = ctx.guild
                channel = ctx.channel
                
                if not guild.voice_client:
                    await user.voice.channel.connect(self_deaf=True)
                    
                self.guildStates[guild.id].queue.put(
                    Item(
                        url=selected['url'], 
                        volume=volume, 
                        user=ctx.author, 
                        title=selected['title']
                    )
                )
                
                embed = discord.Embed(
                    title=Localization.t(ctx, "success"),
                    description=f"**{selected['title']}**を{Localization.t(ctx, 'added_to_queue')}",
                    color=discord.Color.green()
                )
                await message.edit(embed=embed)
                
                if (not self.guildStates[guild.id].playing) and (
                    not self.guildStates[guild.id].alarm
                ):
                    await self.playNext(guild, channel)
            else:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=Localization.t(ctx, "invalid_selection"),
                    color=discord.Color.red()
                )
                await message.edit(embed=embed)
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title=Localization.t(ctx, "timeout"),
                description=Localization.t(ctx, "timeout"),
                color=discord.Color.red()
            )
            await message.edit(embed=embed)

    @searchCommandGroup.command(name="niconico", description="ニコニコ動画から動画を検索して再生します。")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def searchNiconicoCommand(
        self,
        ctx,
        *,
        keyword: str,
        volume: float = 0.5,
    ):
        # 範囲チェック
        if volume < 0.0 or volume > 2.0:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "volume_range"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title=Localization.t(ctx, "loading"),
            description=Localization.t(ctx, "searching").format(keyword),
            color=discord.Color.blue()
        )
        message = await ctx.send(embed=embed)
        
        videos = await searchNicoNico(keyword)
        
        if not videos:
            embed = discord.Embed(
                title="検索結果",
                description=Localization.t(ctx, "search_failed"),
                color=discord.Color.red()
            )
            await message.edit(embed=embed)
            return
        
        # 検索結果の表示
        embed = discord.Embed(
            title="検索結果",
            description=Localization.t(ctx, "select_number"),
            color=discord.Color.blue()
        )
        
        for i, video in enumerate(videos[:5], 1):  # 最大5件まで表示
            embed.add_field(
                name=f"{i}. {video['title']}",
                value=f"{Localization.t(ctx, 'uploaded_by')}: {video['uploader']}",
                inline=False
            )
        
        await message.edit(embed=embed)
        
        # ユーザーの応答を待つ
        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()
        
        try:
            response = await self.bot.wait_for('message', timeout=30.0, check=check)
            choice = int(response.content)
            
            if 1 <= choice <= min(5, len(videos)):
                selected = videos[choice-1]
                
                # 選択された動画を再生
                if not await self.checks(ctx, url=selected['url']):
                    return
                    
                user = ctx.author
                guild = ctx.guild
                channel = ctx.channel
                
                if not guild.voice_client:
                    await user.voice.channel.connect(self_deaf=True)
                    
                self.guildStates[guild.id].queue.put(
                    Item(
                        url=selected['url'], 
                        volume=volume, 
                        user=ctx.author, 
                        title=selected['title']
                    )
                )
                
                embed = discord.Embed(
                    title=Localization.t(ctx, "success"),
                    description=f"**{selected['title']}**を{Localization.t(ctx, 'added_to_queue')}",
                    color=discord.Color.green()
                )
                await message.edit(embed=embed)
                
                if (not self.guildStates[guild.id].playing) and (
                    not self.guildStates[guild.id].alarm
                ):
                    await self.playNext(guild, channel)
            else:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=Localization.t(ctx, "invalid_selection"),
                    color=discord.Color.red()
                )
                await message.edit(embed=embed)
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title=Localization.t(ctx, "timeout"),
                description=Localization.t(ctx, "timeout"),
                color=discord.Color.red()
            )
            await message.edit(embed=embed)

    @tasks.loop(seconds=30)
    async def auto_disconnect(self):
        """アイドル状態が続いた場合、ボットをボイスチャンネルから切断します"""
        for guild_id, state in list(self.guildStates.items()):
            try:
                guild = self.bot.get_guild(guild_id)
                if not guild or not guild.voice_client:
                    continue
                    
                # 再生中でないかつアラームが設定されていない場合
                if not state.playing and not state.alarm:
                    # last_activity属性がなければ追加
                    if not hasattr(state, 'last_activity'):
                        state.last_activity = asyncio.get_event_loop().time()
                        continue
                    
                    # 前回のアクティビティから経過した時間を計算
                    elapsed = asyncio.get_event_loop().time() - state.last_activity
                    
                    # タイムアウト時間を超えた場合は切断
                    if elapsed >= self.idle_timeout:
                        try:
                            channel = guild.voice_client.channel
                            await guild.voice_client.disconnect()
                            print(f"{guild.name}のボイスチャンネル{channel.name}から{self.idle_timeout}秒間のアイドル状態により切断しました")
                            
                            # テキストチャンネルを探してメッセージを送信
                            for text_channel in guild.text_channels:
                                if text_channel.permissions_for(guild.me).send_messages:
                                    embed = discord.Embed(
                                        title="自動切断",
                                        description=f"{self.idle_timeout}秒間何も再生されなかったため、ボイスチャンネルから切断しました。",
                                        color=discord.Color.blue()
                                    )
                                    await text_channel.send(embed=embed)
                                    break
                        except Exception as e:
                            print(f"切断中にエラーが発生: {e}")
                else:
                    # アクティブな場合はタイマーをリセット
                    state.last_activity = asyncio.get_event_loop().time()
            except Exception as e:
                print(f"自動切断処理中にエラーが発生: {e}")
    
    @commands.command(name="timeout", description="自動切断までのアイドル時間を設定します")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def set_timeout(self, ctx, seconds: int = None):
        """自動切断までのアイドル時間を設定します（秒単位、デフォルトは180秒）"""
        if seconds is None:
            embed = discord.Embed(
                title="現在の設定",
                description=f"現在の自動切断までの時間: {self.idle_timeout}秒",
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)
            return
            
        if seconds < 0:
            embed = discord.Embed(
                title="エラー",
                description="タイムアウト時間は0秒以上で指定してください。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
            
        old_timeout = self.idle_timeout
        self.idle_timeout = seconds
        
        embed = discord.Embed(
            title="タイムアウト設定変更",
            description=f"自動切断までの時間を {old_timeout}秒 から {seconds}秒 に変更しました。",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    async def checks(self, ctx, *, url: str = None):
        """コマンド実行前のチェックを行います。"""
        user = ctx.author
        guild = ctx.guild
        
        # アクティビティタイマーをリセット
        if guild.id in self.guildStates:
            self.guildStates[guild.id].last_activity = asyncio.get_event_loop().time()
            
        # ユーザーがVCに接続しているか確認
        if not user.voice:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_in_voice"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False
        permission = ctx.channel.permissions_for(guild.me)
        if (not permission.send_messages) or (not permission.embed_links):
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"このチャンネルの`メッセージを送信`権限と`埋め込みリンク`権限を {self.bot.user.mention} に与えてください。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False
        permission = user.voice.channel.permissions_for(guild.me)
        if not permission.connect:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"ボイスチャンネルの`接続`権限を {self.bot.user.mention} に与えてください。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return False
        if url:
            if "music.apple.com" in url:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description="Apple Musicには対応していません。",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return False
        return True

    @commands.command(name="queue", aliases=["q"], description="キューに入っている曲の一覧を取得します。")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def queueCommand(self, ctx):
        guild = ctx.guild
        if not guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        queue: Queue = self.guildStates[ctx.guild.id].queue
        pageSize = 10
        index = queue.index
        page = (index // pageSize) + 1
        songList: tuple[Item] = queue.pagenation(page, pageSize=pageSize)
        songs = ""
        startIndex = (page - 1) * pageSize

        for i, song in enumerate(songList):
            if startIndex + i == index - 1:
                songs += f"{song.name} by {song.user.mention} (現在再生中)\n"
            else:
                songs += f"{song.name} by {song.user.mention}\n"
        
        # ページネーション情報
        total_pages = (queue.asize() // pageSize) + 1
        pagination_info = f"ページ {page}/{total_pages}"
        
        embed = discord.Embed(
            title=f"キュー",
            description=songs,
            color=discord.Color.blue()
        )
        embed.set_footer(text=pagination_info)
        
        await ctx.send(embed=embed)

    @commands.command(name="skip", aliases=["s"], description="曲をスキップします。")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def skipMusic(self, ctx):
        guild = ctx.guild
        if not guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        self.guildStates[guild.id].playing = False
        guild.voice_client.stop()
        
        embed = discord.Embed(
            title=Localization.t(ctx, "success"),
            description=Localization.t(ctx, "skipped"),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.command(name="stop", aliases=["st"], description="曲を停止します。")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def stopMusic(self, ctx):
        guild = ctx.guild
        if not guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        await guild.voice_client.disconnect()
        self.guildStates[guild.id].playing = False
        self.guildStates[guild.id].alarm = False
        
        embed = discord.Embed(
            title=Localization.t(ctx, "success"),
            description=Localization.t(ctx, "stopped"),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.command(name="pause", aliases=["pa"], description="曲を一時停止します。")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def pauseMusic(self, ctx):
        guild = ctx.guild
        if not guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        if guild.voice_client.is_paused():
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "already_paused"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        guild.voice_client.pause()
        
        embed = discord.Embed(
            title=Localization.t(ctx, "success"),
            description=Localization.t(ctx, "paused"),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.command(name="resume", aliases=["r"], description="一時停止を解除します。")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def resumeMusic(self, ctx):
        guild = ctx.guild
        if not guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        if not guild.voice_client.is_paused():
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_paused"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        guild.voice_client.resume()
        
        embed = discord.Embed(
            title=Localization.t(ctx, "success"),
            description=Localization.t(ctx, "playback_starting"),
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.command(name="seek", aliases=["sk"], description="曲の特定の位置にジャンプします。")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def seekCommand(
        self,
        ctx,
        position: str
    ):
        """指定した位置に曲をジャンプさせます。例: 1:30, 01:30, 01:30:00"""
        if not ctx.guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        # 時間文字列を秒に変換
        try:
            time_parts = position.split(':')
            seconds = 0
            
            if len(time_parts) == 1:
                # 秒のみ
                seconds = int(time_parts[0])
            elif len(time_parts) == 2:
                # 分:秒
                seconds = int(time_parts[0]) * 60 + int(time_parts[1])
            elif len(time_parts) == 3:
                # 時:分:秒
                seconds = int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
            else:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description="無効な時間形式です。例: 1:30, 01:30, 01:30:00",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return
                
            source = ctx.guild.voice_client.source
            max_duration = int(source.info.duration)
            
            if seconds < 0 or seconds > max_duration:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=f"指定した時間が範囲外です。0～{formatTime(max_duration)}の間で指定してください。",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return
            
            # 現在の設定を取得
            guild_id = ctx.guild.id
            bass_boost = self.guildStates[guild_id].bass_boost if hasattr(self.guildStates[guild_id], 'bass_boost') else False
            quality = getattr(self.guildStates[guild_id], 'quality', 'high')
            video_mode = getattr(source, 'video_mode', False) if hasattr(source, 'video_mode') else False
            
            # FFmpegオプションを構築
            options = self.get_audio_options(
                source,
                position=seconds,
                bass_boost=bass_boost,
                quality=quality,
                video_mode=video_mode
            )
                
            ctx.guild.voice_client.source = self.seekMusic(
                source, 
                seconds, 
                bass_boost
            )
            
            embed = discord.Embed(
                title=Localization.t(ctx, "success"),
                description=f"再生位置を{formatTime(seconds)}に移動しました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        except ValueError:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description="無効な時間形式です。例: 1:30, 01:30, 01:30:00",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
        except Exception as e:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"エラーが発生しました: {str(e)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

    @commands.command(name="lyrics", aliases=["ly"], description="現在再生中の曲の歌詞を表示します。")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def lyricsCommand(self, ctx):
        if not ctx.guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        # Genius APIが無効な場合
        if self.genius is None:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description="歌詞表示機能は現在無効です。config.jsonにGenius APIトークンを設定してください。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        embed = discord.Embed(
            title=Localization.t(ctx, "loading"),
            description="歌詞を検索中...",
            color=discord.Color.blue()
        )
        message = await ctx.send(embed=embed)
        
        source = ctx.guild.voice_client.source
        title = source.info.title
        
        # クリーンなタイトルを作成（括弧や特殊文字を削除）
        clean_title = re.sub(r'\([^)]*\)|\[[^\]]*\]|ft\..*|feat\..*|-.*', '', title).strip()
        
        try:
            song = await asyncio.to_thread(self.genius.search_song, clean_title)
            if song:
                lyrics = song.lyrics
                # 歌詞が長すぎる場合は分割
                if len(lyrics) > 4000:
                    parts = [lyrics[i:i+4000] for i in range(0, len(lyrics), 4000)]
                    for i, part in enumerate(parts):
                        embed = discord.Embed(
                            title=f"{title}の歌詞 - パート{i+1}/{len(parts)}",
                            description=part,
                            color=discord.Colour.blue()
                        )
                        if i == 0:
                            await message.edit(embed=embed)
                        else:
                            await ctx.channel.send(embed=embed)
                else:
                    embed = discord.Embed(
                        title=f"{title}の歌詞",
                        description=lyrics,
                        color=discord.Colour.blue()
                    )
                    await message.edit(embed=embed)
            else:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=f"「{clean_title}」の歌詞が見つかりませんでした。",
                    color=discord.Color.red()
                )
                await message.edit(embed=embed)
        except Exception as e:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"), 
                description=f"歌詞の取得中にエラーが発生しました: {str(e)}",
                color=discord.Color.red()
            )
            await message.edit(embed=embed)

    @commands.command(name="bassboost", aliases=["bb"], description="ベースブーストエフェクトを切り替えます。")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def bassboostCommand(self, ctx):
        if not ctx.guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
        
        guild_state = self.guildStates[ctx.guild.id]
        guild_state.bass_boost = not guild_state.bass_boost
        
        source = ctx.guild.voice_client.source
        current_position = source.progress
        
        # 現在の設定を取得
        quality = getattr(guild_state, 'quality', 'high')
        video_mode = getattr(source, 'video_mode', False) if hasattr(source, 'video_mode') else False
        
        # 新しい音質設定で再生
        options = self.get_audio_options(
            source, 
            position=current_position, 
            bass_boost=guild_state.bass_boost,
            quality=quality,
            video_mode=video_mode
        )
        
        # 音源タイプに応じた処理
        if isinstance(source, NicoNicoSource):
            options["before_options"] = (
                f"-headers 'cookie: {'; '.join(f'{k}={v}' for k, v in source.client.cookies.items())}' {options['before_options']}"
            )
            ctx.guild.voice_client.source = NicoNicoSource(
                discord.FFmpegPCMAudio(source.hslContentUrl, executable=FFMPEG_PATH, **options),
                info=source.info,
                hslContentUrl=source.hslContentUrl,
                watchid=source.watchid,
                trackid=source.trackid,
                outputs=source.outputs,
                nicosid=source.nicosid,
                niconico=source.niconico,
                volume=source.volume,
                progress=current_position / 0.02,
                user=source.user,
            )
        elif isinstance(source, DiscordFileSource):
            ctx.guild.voice_client.source = DiscordFileSource(
                discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                info=source.info,
                volume=source.volume,
                progress=current_position / 0.02,
                user=source.user,
            )
        else:
            ctx.guild.voice_client.source = YTDLSource(
                discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                info=source.info,
                volume=source.volume,
                progress=current_position / 0.02,
                user=source.user,
                locale=source.locale if hasattr(source, 'locale') else discord.Locale.japanese,
            )
        
        embed = discord.Embed(
            title=Localization.t(ctx, "success"),
            description=f"ベースブーストを{'有効' if guild_state.bass_boost else '無効'}にしました。",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    @commands.command(name="play", aliases=["p"], description="曲を再生します。")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def playMusic(
        self,
        ctx,
        url: str,
        volume: float = 0.5,
    ):
        # 範囲チェック
        if volume < 0.0 or volume > 2.0:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "volume_range"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        # 基本的なチェック
        try:
            if not await self.checks(ctx, url=url):
                return
        except Exception as e:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"{e}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        user = ctx.author
        guild = ctx.guild
        channel = ctx.channel
        
        # 処理中メッセージを送信
        embed = discord.Embed(
            title=Localization.t(ctx, "loading"),
            description=Localization.t(ctx, "searching").format(url),
            color=discord.Color.blue()
        )
        processing_msg = await ctx.send(embed=embed)

        try:
            # ボイスチャンネルに接続
            if not guild.voice_client:
                await user.voice.channel.connect(self_deaf=True)
                
            # ギルド単位の音質設定を取得
            quality = getattr(self.guildStates[guild.id], 'quality', 'high')
                
            # キューに追加
            await self.putQueue(ctx, url, volume, quality=quality)
            
            # 処理中メッセージを編集
            try:
                embed = discord.Embed(
                    title=Localization.t(ctx, "success"),
                    description=Localization.t(ctx, "added_to_queue"),
                    color=discord.Color.green()
                )
                await processing_msg.edit(embed=embed)
            except:
                pass
                
            # 再生が停止中なら次の曲を再生
            if (not self.guildStates[guild.id].playing) and (
                not self.guildStates[guild.id].alarm
            ):
                await self.playNext(guild, channel)
                
        except Exception as e:
            error_msg = f"{Localization.t(ctx, 'error')}: {e}"
            print(error_msg)
            try:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=error_msg,
                    color=discord.Color.red()
                )
                await processing_msg.edit(embed=embed)
            except:
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=error_msg,
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)

    @commands.command(name="quality", aliases=["hq"], description="音質を設定します")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def setQuality(self, ctx, quality: str = "high"):
        """音質設定を変更します
        
        引数:
            quality: 音質設定 (low/medium/high/ultra)
        """
        if not ctx.guild.voice_client:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_playing"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
            
        # 品質設定の確認と正規化
        quality = quality.lower()
        if quality not in ["low", "medium", "high", "ultra"]:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description="無効な音質設定です。low/medium/high/ultraから選択してください。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
            
        # ギルド状態に音質設定を保存
        guild_id = ctx.guild.id
        if not hasattr(self.guildStates[guild_id], 'quality'):
            self.guildStates[guild_id].quality = quality
        else:
            self.guildStates[guild_id].quality = quality
            
        # 現在再生中なら音質を変更
        if ctx.guild.voice_client.is_playing():
            source = ctx.guild.voice_client.source
            current_position = source.progress
            
            # 現在の設定を取得
            video_mode = getattr(source, 'video_mode', False) if hasattr(source, 'video_mode') else False
            bass_boost = self.guildStates[guild_id].bass_boost if hasattr(self.guildStates[guild_id], 'bass_boost') else False
            
            # 新しい音質設定で再生
            options = self.get_audio_options(
                source, 
                position=current_position, 
                bass_boost=bass_boost,
                quality=quality,
                video_mode=video_mode
            )
            
            # 動画共有モードかどうかで処理を分ける
            if video_mode:
                # 現時点では動画モード中の音質変更はサポートしていない
                embed = discord.Embed(
                    title="注意",
                    description="動画モード中は音質の変更ができません。次の曲から適用されます。",
                    color=discord.Color.yellow()
                )
                await ctx.send(embed=embed)
            else:
                # 音声モードの場合は即時反映
                try:
                    if isinstance(source, NicoNicoSource):
                        options["before_options"] = (
                            f"-headers 'cookie: {'; '.join(f'{k}={v}' for k, v in source.client.cookies.items())}' {options['before_options']}"
                        )
                        ctx.guild.voice_client.source = NicoNicoSource(
                            discord.FFmpegPCMAudio(source.hslContentUrl, executable=FFMPEG_PATH, **options),
                            info=source.info,
                            hslContentUrl=source.hslContentUrl,
                            watchid=source.watchid,
                            trackid=source.trackid,
                            outputs=source.outputs,
                            nicosid=source.nicosid,
                            niconico=source.niconico,
                            volume=source.volume,
                            progress=current_position / 0.02,
                            user=source.user,
                        )
                    elif isinstance(source, DiscordFileSource):
                        ctx.guild.voice_client.source = DiscordFileSource(
                            discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                            info=source.info,
                            volume=source.volume,
                            progress=current_position / 0.02,
                            user=source.user,
                        )
                    else:
                        ctx.guild.voice_client.source = YTDLSource(
                            discord.FFmpegPCMAudio(source.info.url, executable=FFMPEG_PATH, **options),
                            info=source.info,
                            volume=source.volume,
                            progress=current_position / 0.02,
                            user=source.user,
                            locale=source.locale if hasattr(source, 'locale') else discord.Locale.japanese,
                        )
                    
                    embed = discord.Embed(
                        title=Localization.t(ctx, "success"),
                        description=f"音質を **{quality}** に設定しました。",
                        color=discord.Color.green()
                    )
                    await ctx.send(embed=embed)
                except Exception as e:
                    embed = discord.Embed(
                        title=Localization.t(ctx, "error"),
                        description=f"音質変更中にエラーが発生しました: {e}",
                        color=discord.Color.red()
                    )
                    await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title=Localization.t(ctx, "success"),
                description=f"次の曲から音質を **{quality}** に設定します。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)

    @commands.command(name="join", aliases=["j"], description="ボイスチャンネルに参加します")
    @commands.guild_only()
    async def join_command(self, ctx):
        """ボイスチャンネルに参加します"""
        # ユーザーがボイスチャンネルにいるか確認
        if not ctx.author.voice:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "not_in_voice"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
            
        # すでに接続している場合
        if ctx.guild.voice_client:
            # 同じチャンネルなら何もしない
            if ctx.guild.voice_client.channel == ctx.author.voice.channel:
                embed = discord.Embed(
                    title=Localization.t(ctx, "info"),
                    description=f"すでに {ctx.author.voice.channel.mention} に接続しています。",
                    color=discord.Color.blue()
                )
                await ctx.send(embed=embed)
                return
                
            # 違うチャンネルなら移動
            await ctx.guild.voice_client.move_to(ctx.author.voice.channel)
            embed = discord.Embed(
                title=Localization.t(ctx, "success"),
                description=f"{ctx.author.voice.channel.mention} に移動しました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            return
            
        # 新規接続
        try:
            await ctx.author.voice.channel.connect(self_deaf=True)
            embed = discord.Embed(
                title=Localization.t(ctx, "success"),
                description=f"{ctx.author.voice.channel.mention} に接続しました。",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
            # ギルド状態を初期化
            if ctx.guild.id not in self.guildStates:
                self.guildStates[ctx.guild.id] = GuildState()
            
            # アクティビティタイマーをリセット
            self.guildStates[ctx.guild.id].last_activity = asyncio.get_event_loop().time()
            
            # 音楽待機モードを開始（3分間待機）
            if hasattr(self.bot, 'idle_handler'):
                await self.bot.idle_handler.start_music_wait(
                    ctx.guild.id, 
                    ctx.guild.voice_client, 
                    ctx.channel
                )
        except Exception as e:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"ボイスチャンネルへの接続に失敗しました: {str(e)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

    @commands.command(name="playlist", aliases=["pl"], description="プレイリストを再生します。")
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def playPlaylist(
        self,
        ctx,
        url: str,
        volume: float = 0.5,
    ):
        """プレイリスト全体を再生キューに追加します
        
        引数:
            url: プレイリストのURL (YouTube、Spotify対応)
            volume: 音量 (0.0-2.0)
        """
        # 範囲チェック
        if volume < 0.0 or volume > 2.0:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=Localization.t(ctx, "volume_range"),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        # URLチェック
        if "playlist" not in url and "list=" not in url and "album" not in url and "spotify.com" not in url:
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description="これはプレイリストURLではないようです。通常の再生コマンド（`d!play`）を使用してください。",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return

        # 基本的なチェック
        if not await self.checks(ctx, url=url):
            return

        user = ctx.author
        guild = ctx.guild
        channel = ctx.channel
        
        # 処理中メッセージを送信
        embed = discord.Embed(
            title="プレイリスト読み込み中",
            description=f"プレイリスト: {url}\n\n読み込みには時間がかかる場合があります...",
            color=discord.Color.blue()
        )
        processing_msg = await ctx.send(embed=embed)

        try:
            # ボイスチャンネルに接続
            if not guild.voice_client:
                await user.voice.channel.connect(self_deaf=True)
            
            # 新しいSourceFactoryの使用を試みる
            try:
                from musicbot_source.factory import SourceFactory
                has_factory = True
            except ImportError:
                has_factory = False
            
            if has_factory:
                # SourceFactoryを使用したプレイリスト検出
                playlist_info = await SourceFactory.detect_playlist(url, discord.Locale.japanese)
                
                if playlist_info['is_playlist']:
                    if playlist_info['platform'] == 'spotify':
                        # Spotifyプレイリスト
                        await self.handle_spotify_playlist(ctx, url, volume, processing_msg)
                    else:
                        # YouTube/その他のプレイリスト
                        items_count = await self.putQueue(ctx, url, volume)
                        
                        if items_count > 0:
                            embed = discord.Embed(
                                title="プレイリスト追加完了",
                                description=f"**{items_count}曲**をキューに追加しました。",
                                color=discord.Color.green()
                            )
                            await processing_msg.edit(embed=embed)
                            
                            # 再生が停止中なら次の曲を再生
                            if (not self.guildStates[guild.id].playing) and (
                                not self.guildStates[guild.id].alarm
                            ):
                                await self.playNext(guild, channel)
                        else:
                            embed = discord.Embed(
                                title=Localization.t(ctx, "error"),
                                description="プレイリストから曲を追加できませんでした。",
                                color=discord.Color.red()
                            )
                            await processing_msg.edit(embed=embed)
                else:
                    # プレイリストではない場合は通常の再生
                    await self.putQueue(ctx, url, volume)
                    
                    embed = discord.Embed(
                        title="曲を追加しました",
                        description="プレイリストではなく、単一の曲をキューに追加しました。",
                        color=discord.Color.green()
                    )
                    await processing_msg.edit(embed=embed)
                    
                    # 再生が停止中なら次の曲を再生
                    if (not self.guildStates[guild.id].playing) and (
                        not self.guildStates[guild.id].alarm
                    ):
                        await self.playNext(guild, channel)
            else:
                # 従来の方法でプレイリスト処理
                items_count = await self.putQueue(ctx, url, volume)
                
                if items_count > 0:
                    embed = discord.Embed(
                        title="プレイリスト追加完了",
                        description=f"**{items_count}曲**をキューに追加しました。",
                        color=discord.Color.green()
                    )
                    await processing_msg.edit(embed=embed)
                    
                    # 再生が停止中なら次の曲を再生
                    if (not self.guildStates[guild.id].playing) and (
                        not self.guildStates[guild.id].alarm
                    ):
                        await self.playNext(guild, channel)
                else:
                    embed = discord.Embed(
                        title=Localization.t(ctx, "error"),
                        description="プレイリストから曲を追加できませんでした。",
                        color=discord.Color.red()
                    )
                    await processing_msg.edit(embed=embed)
                
        except Exception as e:
            error_msg = f"{Localization.t(ctx, 'error')}: {e}"
            print(f"プレイリスト処理エラー: {error_msg}")
            
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"プレイリスト処理中にエラーが発生しました: {str(e)}",
                color=discord.Color.red()
            )
            await processing_msg.edit(embed=embed)
    
    async def handle_spotify_playlist(self, ctx, url, volume, message=None):
        """Spotifyプレイリストを処理する専用メソッド"""
        # Spotifyが無効化されている場合
        if not hasattr(self.spotify, 'downloader') or not callable(getattr(self.spotify.downloader, 'search', None)):
            embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description="Spotify機能は現在無効です。config.jsonにSpotify APIキーを設定してください。",
                color=discord.Color.red()
            )
            if message:
                await message.edit(embed=embed)
            else:
                await ctx.send(embed=embed)
            return 0
            
        queue = self.guildStates[ctx.guild.id].queue
        titles = {}
        songs = []

        # 進捗メッセージの準備
        progress_embed = discord.Embed(
            title="Spotifyプレイリスト処理中",
            description="プレイリスト情報を取得しています...",
            color=discord.Color.blue()
        )
        if message:
            await message.edit(embed=progress_embed)
        else:
            message = await ctx.send(embed=progress_embed)
            
        try:
            # 最新版のSpotdlライブラリのAPIに対応
            progress_embed.description = "Spotify情報を取得しています..."
            await message.edit(embed=progress_embed)
            
            # 直接spotdlライブラリを使ってURLから曲情報を取得
            try:
                # SpotdlのAPIバージョンに基づいて適切なメソッドを呼び出す
                print(f"Spotify URL処理開始: {url}")
                
                # 必要に応じて検索メソッドを選択
                if hasattr(self.spotify, 'search'):
                    # 新しいバージョンのSpotdl
                    print("新バージョンSpotdl検索使用")
                    songs_info = await asyncio.to_thread(lambda: self.spotify.search([url]))
                elif hasattr(self.spotify, 'spotify_client') and hasattr(self.spotify.spotify_client, 'get_track_info'):
                    # 旧バージョンのSpotdl
                    print("旧バージョンSpotdl検索使用")
                    if "track" in url:
                        track = await asyncio.to_thread(self.spotify.spotify_client.get_track_info, url)
                        songs_info = [track]
                    elif "album" in url:
                        album = await asyncio.to_thread(self.spotify.spotify_client.get_album_info, url)
                        songs_info = album.tracks if hasattr(album, 'tracks') else getattr(album, 'songs', [])
                    elif "playlist" in url:
                        playlist = await asyncio.to_thread(self.spotify.spotify_client.get_playlist_info, url)
                        songs_info = playlist.tracks if hasattr(playlist, 'tracks') else getattr(playlist, 'songs', [])
                    else:
                        # 単一トラックとして扱う
                        track = await asyncio.to_thread(self.spotify.spotify_client.get_track_info, url)
                        songs_info = [track]
                else:
                    # Spotify機能が無効
                    print("Spotify機能が無効です")
                    embed = discord.Embed(
                        title=Localization.t(ctx, "error"),
                        description="Spotify機能が正しく初期化されていません。",
                        color=discord.Color.red()
                    )
                    await message.edit(embed=embed)
                    return 0
                
                # 曲情報の存在チェック
                if not songs_info or len(songs_info) == 0:
                    print("Spotify曲情報が取得できませんでした")
                    embed = discord.Embed(
                        title=Localization.t(ctx, "error"),
                        description="曲情報を取得できませんでした。",
                        color=discord.Color.red()
                    )
                    await message.edit(embed=embed)
                    return 0
                
                print(f"取得した曲数: {len(songs_info)}")
                    
                # 曲情報を整理
                for song in songs_info:
                    # 辞書形式の場合
                    if isinstance(song, dict):
                        song_id = song.get('song_id', '') or song.get('url', '')
                        if song_id:
                            titles[song_id] = song.get('name', 'Unknown Title')
                    # クラスインスタンスの場合
                    else:
                        try:
                            song_id = getattr(song, 'song_id', '') or getattr(song, 'url', '')
                            if song_id:
                                titles[song_id] = getattr(song, 'name', 'Unknown Title')
                        except Exception as e:
                            print(f"Songオブジェクト処理エラー: {e}")
                
                songs = songs_info
                
                # コレクション情報を表示（可能であれば）
                name = "Spotifyコレクション"
                count = len(songs)
                if "track" in url:
                    name = "Spotify トラック"
                elif "album" in url:
                    name = "Spotify アルバム"
                elif "playlist" in url:
                    name = "Spotify プレイリスト"
                
                progress_embed.description = f"{name}の{count}曲を処理中..."
                await message.edit(embed=progress_embed)
            except Exception as e:
                print(f"Spotify情報取得エラー: {e}")
                embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description=f"Spotify情報の取得に失敗しました: {str(e)}",
                    color=discord.Color.red()
                )
                await message.edit(embed=embed)
                return 0
                
            # 曲数が多い場合は警告
            if len(songs) > 50:
                warning_embed = discord.Embed(
                    title="⚠️ 注意",
                    description=f"プレイリストに{len(songs)}曲あります。処理に時間がかかる場合があります。",
                    color=discord.Color.gold()
                )
                await message.edit(embed=warning_embed)
                
            # YouTube検索用URLを取得
            progress_embed.description = f"{len(songs)}曲のYouTube情報を取得中..."
            await message.edit(embed=progress_embed)
            
            urls, failed_songs = await asyncio.to_thread(self.getDownloadUrls, songs)
            
            # 失敗した曲を除外
            for song_id in failed_songs:
                if song_id in titles:
                    del titles[song_id]
                    
            # キューに追加
            for url, song_id in urls:
                # song_idがtitlesに存在しない場合のエラー回避
                if song_id not in titles:
                    print(f"警告: song_id '{song_id}' のタイトルが見つかりません")
                    title = f"Spotify曲 ({song_id})"
                else:
                    title = titles[song_id]
                    
                # キューに追加
                queue.put(
                    Item(
                        url=url,
                        volume=volume,
                        user=ctx.author,
                        title=title,
                        locale=discord.Locale.japanese,
                        video_mode=False,
                        quality="high",
                    )
                )
                print(f"Spotify曲をキューに追加: {title}")
                
            # 結果の表示
            if len(urls) > 0:
                result_embed = discord.Embed(
                    title="Spotifyプレイリスト追加完了",
                    description=f"**{len(urls)}曲**をキューに追加しました。",
                    color=discord.Color.green()
                )
                if len(failed_songs) > 0:
                    result_embed.add_field(
                        name="注意", 
                        value=f"{len(failed_songs)}曲は処理できませんでした。",
                        inline=False
                    )
                await message.edit(embed=result_embed)
                return len(urls)
            else:
                error_embed = discord.Embed(
                    title=Localization.t(ctx, "error"),
                    description="プレイリストから曲を追加できませんでした。",
                    color=discord.Color.red()
                )
                await message.edit(embed=error_embed)
                return 0
                
        except Exception as e:
            error_embed = discord.Embed(
                title=Localization.t(ctx, "error"),
                description=f"Spotifyプレイリスト処理中にエラーが発生しました: {str(e)}",
                color=discord.Color.red()
            )
            await message.edit(embed=error_embed)
            return 0


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
