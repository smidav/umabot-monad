"""
bot.py — Discord bot for tracking Umamusume enemy character appearances and losses.

Flow after OCR
──────────────
1. Results summary (one message, edited in-place from the "Analysing..." message)
2. Intermediate alt pages if needed (each deleted after commit)
3. Final page: up to 3 alt selects + winner select + Confirm button
   (edited in-place to show the saved result on confirm)

Commands:  !uma help | tally | export | undo | reset
"""

import io
import os
import asyncio
import discord
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv

from ocr import extract_race_data, CharacterEntry
from tally import Tally
from constants import ALTS_DICT

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID    = int(os.environ["CHANNEL_ID"]) if os.environ.get("CHANNEL_ID") else None
ADMIN_ID      = int(os.environ["ADMIN_ID"])    if os.environ.get("ADMIN_ID")    else None

ALT_TIMEOUT  = 600.0   # 10 minutes
ALTS_PER_MID = 4       # selects per intermediate page
ALTS_ON_LAST = 3       # selects on final page (row 3 = winner, row 4 = confirm)
WINNER_KEY   = "__winner__"

# ── Bot setup ─────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot   = commands.Bot(command_prefix="!uma ", intents=intents, help_command=None)
tally = Tally()

_last_entries:    list[CharacterEntry] = []
_last_race_added: bool                 = False


# ── Alt select ────────────────────────────────────────────────────────────────

class AltSelect(ui.Select):
    def __init__(self, entry: CharacterEntry, shared: dict, row: int):
        alts = ALTS_DICT[entry.name]["alts"]

        def label(alt: str) -> str:
            return f"Gate {entry.gate}: {entry.name}" if alt == "Base" \
                   else f"Gate {entry.gate}: {entry.name}-{alt}"

        super().__init__(
            placeholder=f"Gate {entry.gate}: {entry.name}",
            options=[discord.SelectOption(label=label(a), value=a) for a in alts.values()],
            row=row,
        )
        self.gate   = entry.gate
        self.shared = shared
        shared[entry.gate] = "Base"

    async def callback(self, interaction: discord.Interaction):
        self.shared[self.gate] = self.values[0]
        await interaction.response.defer()


# ── Winner select ─────────────────────────────────────────────────────────────

class WinnerSelect(ui.Select):
    def __init__(self, entries: list[CharacterEntry], shared: dict):
        options = [discord.SelectOption(label="Player win", value="__win__")]
        for e in entries:
            options.append(discord.SelectOption(
                label=f"Gate {e.gate}: {e.name}",
                value=str(e.gate),
            ))
        super().__init__(
            placeholder="Race result",
            options=options,
            row=3,
        )
        self.shared = shared
        shared[WINNER_KEY] = "__win__"

    async def callback(self, interaction: discord.Interaction):
        self.shared[WINNER_KEY] = self.values[0]
        await interaction.response.defer()


# ── Page views ────────────────────────────────────────────────────────────────

class MidPageView(ui.View):
    """Intermediate page: up to 4 alt selects. Deleted on commit."""

    def __init__(self, selects: list[AltSelect]):
        super().__init__(timeout=ALT_TIMEOUT)
        for sel in selects:
            self.add_item(sel)
        self.add_item(ui.Button(
            label="continued below",
            style=discord.ButtonStyle.secondary,
            disabled=True,
            row=4,
        ))


class FinalPageView(ui.View):
    def __init__(
        self,
        alt_selects:    list[AltSelect],
        winner_select:  WinnerSelect,
        all_entries:    list[CharacterEntry],
        shared:         dict,
        mid_messages:   list[discord.Message],
        status_message: discord.Message,
    ):
        super().__init__(timeout=ALT_TIMEOUT)
        self.all_entries    = all_entries
        self.shared         = shared
        self.mid_messages   = mid_messages
        self.status_message = status_message
        self.confirmed      = False

        for sel in alt_selects:
            self.add_item(sel)
        self.add_item(winner_select)

        btn = ui.Button(label="Confirm & Save", style=discord.ButtonStyle.success, row=4)
        btn.callback = self._confirm
        self.add_item(btn)

    async def _confirm(self, interaction: discord.Interaction):
        self.confirmed = True
        self.stop()
        await _commit(self.all_entries, self.shared, interaction, self.mid_messages, self.status_message)

    async def on_timeout(self):
        if not self.confirmed:
            await _commit(self.all_entries, self.shared, None, self.mid_messages, self.status_message)


# ── Tally commit ──────────────────────────────────────────────────────────────

async def _commit(
    entries:        list[CharacterEntry],
    shared:         dict,
    interaction:    discord.Interaction | None,
    mid_messages:   list[discord.Message],
    status_message: discord.Message,
):
    global _last_entries, _last_race_added

    for entry in entries:
        alt = shared.get(entry.gate, "Base")
        entry.alt = alt
        if alt != "Base":
            entry.name = f"{entry.name}-{alt}"

    winner_raw = shared.get(WINNER_KEY, "__win__")
    if winner_raw == "__win__":
        winner_name = None
    else:
        gate = int(winner_raw)
        winner_entry = next((e for e in entries if e.gate == gate), None)
        winner_name  = winner_entry.name if winner_entry else None

    tally.record_entries(entries, winner_name)
    _last_entries    = entries
    _last_race_added = True

    # Delete intermediate alt pages and the selection message
    for msg in mid_messages:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
    if interaction is not None:
        try:
            await interaction.response.defer()
            await interaction.message.delete()
        except discord.HTTPException:
            pass

    # Edit the original status message to the final result
    result = "Player win" if winner_name is None else f"Lost to {winner_name}"
    lines  = [f"{result}  —  {tally.total_races_processed} race(s) recorded\n"]
    for e in entries:
        lines.append(f"Gate {e.gate}  {e.name}  {e.strategy}")
    try:
        await status_message.edit(content="\n".join(lines))
    except discord.HTTPException:
        pass


# ── Image processing ──────────────────────────────────────────────────────────

def is_image_attachment(a: discord.Attachment) -> bool:
    return a.content_type is not None and a.content_type.startswith("image/")


async def process_image(message: discord.Message, attachment: discord.Attachment):
    status = await message.reply("Analysing race screenshot...", mention_author=False)

    try:
        entries = extract_race_data(attachment.url)
    except Exception as e:
        await status.edit(content=f"OCR failed: {e}")
        return
    if not entries:
        await status.edit(content="No enemy characters could be extracted.")
        return

    shared: dict = {}

    alt_cards = [
        e for e in entries
        if e.name in ALTS_DICT and len(ALTS_DICT[e.name]["alts"]) >= 2
    ]

    mid_messages: list[discord.Message] = []
    if len(alt_cards) > ALTS_ON_LAST:
        mid_cards  = alt_cards[:-ALTS_ON_LAST]
        final_alts = alt_cards[-ALTS_ON_LAST:]
        for i in range(0, len(mid_cards), ALTS_PER_MID):
            batch = mid_cards[i:i + ALTS_PER_MID]
            sels  = [AltSelect(e, shared, row=r) for r, e in enumerate(batch)]
            msg   = await message.channel.send(
                "Alt selection (continued below)",
                view=MidPageView(sels),
            )
            mid_messages.append(msg)
    else:
        final_alts = alt_cards

    final_alt_sels = [AltSelect(e, shared, row=r) for r, e in enumerate(final_alts)]
    winner_sel     = WinnerSelect(entries, shared)
    await message.channel.send(
        f"Select alts and race result ({int(ALT_TIMEOUT//60)} min timeout)",
        view=FinalPageView(final_alt_sels, winner_sel, entries, shared, mid_messages, status),
    )


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Channel: {'all' if not CHANNEL_ID else CHANNEL_ID}")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if CHANNEL_ID and message.channel.id != CHANNEL_ID:
        await bot.process_commands(message)
        return
    if message.attachments:
        images = [a for a in message.attachments if is_image_attachment(a)]
        for attachment in images[:1]:
            await process_image(message, attachment)
        return
    await bot.process_commands(message)


# ── Commands ──────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    lines = [
        "**Umamusume Tally Bot**",
        "",
        "Post a race lineup screenshot to record it. The bot reads gate numbers,",
        "character names, and strategies. You'll then be prompted to select alts",
        "and the race result before confirming.",
        "",
        "`!uma tally`  — view standings",
        "`!uma export` — download CSV",
        "`!uma undo`   — remove the last race",
        "`!uma reset`  — wipe all data (admin only)",
    ]
    await ctx.send("\n".join(lines))


@bot.command(name="tally")
async def tally_cmd(ctx: commands.Context):
    text = tally.format_tally_text(top_n=25)
    if len(text) > 1900:
        text = text[:1897] + "..."
    await ctx.send(f"```\n{text}\n```")


@bot.command(name="export")
async def export_cmd(ctx: commands.Context):
    if not tally.characters:
        await ctx.send("No data to export yet.")
        return
    file = discord.File(
        fp=io.BytesIO(tally.to_csv().encode("utf-8")),
        filename="umamusume_tally.csv",
    )
    await ctx.send(
        f"Export: {tally.total_races_processed} race(s), "
        f"{len(tally.characters)} unique characters.",
        file=file,
    )


@bot.command(name="undo")
async def undo_cmd(ctx: commands.Context):
    global _last_race_added
    if not _last_race_added or not _last_entries:
        await ctx.send("Nothing to undo.")
        return

    for entry in _last_entries:
        key = entry.name.lower()
        if key in tally.characters:
            stats = tally.characters[key]
            stats.total -= 1
            if entry.strategy in stats.strategies:
                stats.strategies[entry.strategy] -= 1
                if stats.strategies[entry.strategy] <= 0:
                    del stats.strategies[entry.strategy]
            if stats.total <= 0:
                del tally.characters[key]

    if tally.last_winner:
        key = tally.last_winner.lower()
        if key in tally.characters:
            tally.characters[key].losses = max(0, tally.characters[key].losses - 1)

    tally.total_races_processed = max(0, tally.total_races_processed - 1)
    tally.save()
    _last_race_added = False
    await ctx.send(f"Undone. Removed: {', '.join(f'Gate {e.gate} {e.name}' for e in _last_entries)}.")


@bot.command(name="reset")
async def reset_cmd(ctx: commands.Context):
    if ADMIN_ID and ctx.author.id != ADMIN_ID:
        await ctx.send("Only the configured admin can reset the tally.")
        return
    await ctx.send(
        "This will wipe all tally data. "
        "Reply with `!uma reset confirm` within 15 seconds to proceed."
    )
    def check(m):
        return (m.author == ctx.author and m.channel == ctx.channel
                and m.content.strip().lower() == "!uma reset confirm")
    try:
        await bot.wait_for("message", check=check, timeout=15.0)
    except asyncio.TimeoutError:
        await ctx.send("Reset cancelled.")
        return
    tally.reset()
    await ctx.send("Tally wiped.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
