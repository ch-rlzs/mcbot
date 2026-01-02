import os
import re
import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ─────────────────────────────
# ENV + CONFIG
# ─────────────────────────────

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
DB_PATH = os.path.join("data", "watches.db")

MC_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,16}$")

os.makedirs("data", exist_ok=True)

# ─────────────────────────────
# DATABASE
# ─────────────────────────────

def db_connect():
    return sqlite3.connect(DB_PATH)

def db_init():
    with db_connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            guild_id     INTEGER NOT NULL,
            channel_id   INTEGER NOT NULL,
            mc_name      TEXT    NOT NULL,
            last_status  TEXT    NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (guild_id, channel_id, mc_name)
        )
        """)
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_watches_mc_name ON watches(mc_name)"
        )
        con.commit()

def db_add_watch(guild_id: int, channel_id: int, mc_name: str):
    with db_connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO watches (guild_id, channel_id, mc_name, last_status) VALUES (?, ?, ?, 'unknown')",
            (guild_id, channel_id, mc_name.lower())
        )
        con.commit()

def db_remove_watch(guild_id: int, channel_id: int, mc_name: str) -> int:
    with db_connect() as con:
        cur = con.execute(
            "DELETE FROM watches WHERE guild_id=? AND channel_id=? AND mc_name=?",
            (guild_id, channel_id, mc_name.lower())
        )
        con.commit()
        return cur.rowcount

def db_list_watches(guild_id: int, channel_id: int) -> list[str]:
    with db_connect() as con:
        cur = con.execute(
            "SELECT mc_name FROM watches WHERE guild_id=? AND channel_id=? ORDER BY mc_name ASC",
            (guild_id, channel_id)
        )
        return [r[0] for r in cur.fetchall()]

@dataclass
class WatchRow:
    guild_id: int
    channel_id: int
    mc_name: str
    last_status: str

def db_get_all_watches() -> list[WatchRow]:
    with db_connect() as con:
        cur = con.execute(
            "SELECT guild_id, channel_id, mc_name, last_status FROM watches"
        )
        return [WatchRow(*r) for r in cur.fetchall()]

def db_update_status(guild_id: int, channel_id: int, mc_name: str, status: str):
    with db_connect() as con:
        con.execute(
            "UPDATE watches SET last_status=? WHERE guild_id=? AND channel_id=? AND mc_name=?",
            (status, guild_id, channel_id, mc_name.lower())
        )
        con.commit()

# ─────────────────────────────
# MOJANG CHECK
# ─────────────────────────────

async def mojang_name_exists(session: aiohttp.ClientSession, name: str) -> Optional[bool]:
    url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return True
            if resp.status == 204:
                return False
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

# ─────────────────────────────
# BOT
# ─────────────────────────────

intents = discord.Intents.default()

class NameWatchBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.bg_task: Optional[asyncio.Task] = None

    async def setup_hook(self):
        self.tree.add_command(watch_cmd)
        self.tree.add_command(unwatch_cmd)
        self.tree.add_command(listwatches_cmd)

        await self.tree.sync()
        print("Slash commands synced")

        self.bg_task = asyncio.create_task(self.watch_loop())

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print(f"Bot user ID: {self.user.id}")
        print(f"Guild count: {len(self.guilds)}")
        print("Guilds:", [g.name for g in self.guilds])

    async def watch_loop(self):
        await self.wait_until_ready()
        interval = max(5, CHECK_INTERVAL_MINUTES)
        print(f"Watch loop running every {interval} minutes")

        async with aiohttp.ClientSession(headers={"User-Agent": "mc-name-watch-bot"}) as session:
            while not self.is_closed():
                rows = db_get_all_watches()
                for row in rows:
                    exists = await mojang_name_exists(session, row.mc_name)
                    if exists is None:
                        continue

                    status = "taken" if exists else "available"
                    if status != row.last_status:
                        db_update_status(
                            row.guild_id, row.channel_id, row.mc_name, status
                        )
                        await self.notify_change(
                            row.channel_id, row.mc_name, status
                        )

                    await asyncio.sleep(1.2)

                await asyncio.sleep(interval * 60)

    async def notify_change(self, channel_id: int, name: str, status: str):
        channel = self.get_channel(channel_id)
        if not channel:
            return

        if status == "available":
            msg = f"@here 🚨 **{name}** looks **AVAILABLE** right now."
        else:
            msg = f"ℹ️ **{name}** is currently taken."

        try:
            await channel.send(msg)
        except discord.Forbidden:
            pass

bot = NameWatchBot()

# ─────────────────────────────
# SLASH COMMANDS (WITH LOGGING)
# ─────────────────────────────

@app_commands.command(name="watch", description="Watch a Minecraft username.")
async def watch_cmd(interaction: discord.Interaction, name: str):
    print(f"[WATCH] user={interaction.user} guild={interaction.guild_id} channel={interaction.channel_id} name={name}")

    if interaction.guild is None:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)

    name = name.strip()
    if not MC_NAME_RE.match(name):
        return await interaction.response.send_message("Invalid Minecraft name.", ephemeral=True)

    db_add_watch(interaction.guild_id, interaction.channel_id, name)
    await interaction.response.send_message(f"Watching **{name}**.", ephemeral=True)

@app_commands.command(name="unwatch", description="Stop watching a Minecraft username.")
async def unwatch_cmd(interaction: discord.Interaction, name: str):
    print(f"[UNWATCH] user={interaction.user} guild={interaction.guild_id} channel={interaction.channel_id} name={name}")

    removed = db_remove_watch(interaction.guild_id, interaction.channel_id, name)
    if removed:
        await interaction.response.send_message(f"Stopped watching **{name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"**{name}** was not being watched.", ephemeral=True)

@app_commands.command(name="listwatches", description="List watched names in this channel.")
async def listwatches_cmd(interaction: discord.Interaction):
    print(f"[LIST] user={interaction.user} guild={interaction.guild_id} channel={interaction.channel_id}")

    names = db_list_watches(interaction.guild_id, interaction.channel_id)
    if not names:
        return await interaction.response.send_message("No watched names.", ephemeral=True)

    await interaction.response.send_message(
        "Watched names:\n" + "\n".join(f"• {n}" for n in names),
        ephemeral=True
    )

# ─────────────────────────────
# ENTRY
# ─────────────────────────────

if __name__ == "__main__":
    db_init()
    bot.run(TOKEN)
