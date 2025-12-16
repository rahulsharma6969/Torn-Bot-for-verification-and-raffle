import os
import json
import discord
import requests
import asyncio
from discord.ext import commands, tasks
from discord import app_commands

# ================= CONFIGURATION =================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
HOST_API_KEY = os.environ["HOST_API_KEY"]   # recepient api key
HOST_TORN_ID = "xxxxxxxx"         # torn id of account receiving items
LOG_CHANNEL_ID = xxxxxxxxx                # channel id where bot is running "general right now"

RAFFLE_CONFIG = {
    "TICKET_PRICE": 400000,      # value of 1 ticket
    "TRIGGER_MSG": "LLF",        # Message user must send with item
    "LOG_ID": 4103,              # id for receiving item
}

# File Names
LINKS_FILE = "linked_users.json"
RAFFLE_FILE = "raffle_data.json"
PRICES_FILE = "item_prices_cache.json"

# ================= HELPER FUNCTIONS =================
def load_json(filename, default):
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"⚠️ Error reading {filename}, using default.")
        return default

def save_json(filename, data):
    # Safe write: write to temp file then rename to prevent corruption
    temp = filename + ".tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(temp, filename)

linked_users = load_json(LINKS_FILE, {})
raffle_data = load_json(RAFFLE_FILE, {
    "meta": {"last_log_ts": 0, "total_pool_value": 0},
    "tickets": {}
})
item_prices = load_json(PRICES_FILE, {})

# ================= PRICE FETCHER =================
async def update_item_prices():
    """
    Fetches all items from Torn Official API.
    Logic: Use 'market_value'. If 0 (like Gold AK), use 'buy_price'.
    """
    print("🔄 Fetching latest item prices from Torn API...")
    url = f"https://api.torn.com/torn/?selections=items&key={HOST_API_KEY}"
    
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, requests.get, url)
        data = response.json()
        
        if 'error' in data:
            print(f"❌ Failed to fetch prices: {data['error']}")
            return

        items = data.get('items', {})
        new_prices = {}
        
        for i_id, i_data in items.items():
            market_val = i_data.get('market_value', 0)
            buy_price = i_data.get('buy_price', 0)
            
            # If market is 0, fall back to NPC Buy Price
            final_price = market_val if market_val > 0 else buy_price
            
            new_prices[str(i_id)] = final_price

        global item_prices
        item_prices = new_prices
        save_json(PRICES_FILE, item_prices)
        print(f"✅ Updated prices for {len(item_prices)} items.")
        
    except Exception as e:
        print(f"❌ Error updating prices: {e}")

# ================= BOT SETUP =================
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    
    # 1. Fetch prices immediately if cache is empty
    if not item_prices:
        await update_item_prices()
    
    try:
        await bot.tree.sync()
        
        # 2. Start Background Tasks
        if not check_donations.is_running():
            check_donations.start()
            print("👀 Donation watcher started...")
            
        if not price_updater_task.is_running():
            price_updater_task.start()
            print("💹 Price updater started...")
            
    except Exception as e:
        print(f"Startup Error: {e}")

# ================= BACKGROUND TASKS =================

@tasks.loop(hours=6)
async def price_updater_task():
    # Keep prices fresh every 6 hours
    await update_item_prices()

@tasks.loop(seconds=60)
async def check_donations():
    last_ts = raffle_data["meta"]["last_log_ts"]
    
    # Fetch Logs (Read-Only)
    url = f"https://api.torn.com/user/{HOST_TORN_ID}?selections=log&key={HOST_API_KEY}&limit=50"
    
    try:
        r = requests.get(url)
        data = r.json()
    except Exception as e:
        print(f"API Request Failed: {e}")
        return

    if 'error' in data:
        print(f"API Error: {data['error']}")
        return

    # Process oldest logs first
    logs = sorted(data.get('log', {}).values(), key=lambda x: x['timestamp'])
    updates_made = False
    
    for entry in logs:
        # 1. Skip old logs
        if entry['timestamp'] <= last_ts:
            continue
            
        # Update cursor so we don't process this again
        raffle_data["meta"]["last_log_ts"] = entry['timestamp']
        updates_made = True

        # 2. Verify Log Type (4103) & Trigger Message ("LLF")
        if entry['log'] != RAFFLE_CONFIG['LOG_ID']:
            continue

        msg = entry.get('data', {}).get('message', '')
        if RAFFLE_CONFIG['TRIGGER_MSG'] not in msg:
            continue

        # 3. Calculate Value
        sender_id = str(entry['data']['sender'])
        items_list = entry['data'].get('items', [])
        
        total_entry_value = 0
        
        for item_obj in items_list:
            i_id = str(item_obj.get('id'))
            i_qty = item_obj.get('qty')
            
            # Lookup price (default to 0 if unknown)
            price = item_prices.get(i_id, 0)
            total_entry_value += (price * i_qty)

        # 4. Award Tickets
        # Integer division: 835k // 400k = 2 tickets
        tickets_earned = int(total_entry_value // RAFFLE_CONFIG['TICKET_PRICE'])

        if tickets_earned > 0:
            current = raffle_data["tickets"].get(sender_id, 0)
            raffle_data["tickets"][sender_id] = current + tickets_earned
            raffle_data["meta"]["total_pool_value"] += total_entry_value
            
            # Find Discord User for the ping
            discord_id = None
            for d_id, t_id in linked_users.items():
                if str(t_id) == sender_id:
                    discord_id = d_id
                    break
            
            # Post to Discord
            channel = bot.get_channel(LOG_CHANNEL_ID)
            if channel:
                mention = f"<@{discord_id}>" if discord_id else f"User [{sender_id}]"
                await channel.send(
                    f"🎟️ **TICKET UPDATE**\n"
                    f"{mention} sent items worth **${total_entry_value:,}**\n"
                    f"**+{tickets_earned} Tickets** (Total: {raffle_data['tickets'][sender_id]})"
                )
        else:
            # Value was too low (Trash/Donation)
            print(f"User {sender_id} sent items worth ${total_entry_value} (0 Tickets)")

    if updates_made:
        save_json(RAFFLE_FILE, raffle_data)

# ================= COMMANDS =================
@bot.tree.command(name="link", description="Link your Torn account manually.")
async def link(interaction: discord.Interaction, torn_id: int):
    # update linked_users.json
    linked_users[str(interaction.user.id)] = torn_id
    save_json(LINKS_FILE, linked_users)
    await interaction.response.send_message(f"✅ Linked to Torn ID: {torn_id}", ephemeral=True)

@bot.tree.command(name="tickets", description="Check your current raffle tickets.")
async def tickets(interaction: discord.Interaction):
    user_torn_id = linked_users.get(str(interaction.user.id))
    if not user_torn_id:
        await interaction.response.send_message("❌ Link your account first with `/link`!", ephemeral=True)
        return
    
    count = raffle_data["tickets"].get(str(user_torn_id), 0)
    await interaction.response.send_message(f"🎟️ You have **{count}** tickets!", ephemeral=True)

@bot.tree.command(name="pot", description="View raffle statistics.")
async def pot(interaction: discord.Interaction):
    total_val = raffle_data["meta"]["total_pool_value"]
    total_tix = sum(raffle_data["tickets"].values())
    participants = len(raffle_data["tickets"])
    
    await interaction.response.send_message(
        f"📊 **Current Raffle Pot**\n"
        f"💰 Total Value: ${total_val:,}\n"
        f"🎟️ Total Tickets: {total_tix:,}\n"
        f"👤 Participants: {participants}"
    )

@bot.tree.command(name="update_prices", description="Admin: Force update item prices.")
@app_commands.checks.has_permissions(administrator=True)
async def force_update(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_item_prices()
    await interaction.followup.send(f"✅ Prices updated! Tracking {len(item_prices)} items.")


@bot.tree.command(name="reset_raffle", description="Admin: Start a NEW raffle round (Flushes tickets).")
@app_commands.checks.has_permissions(administrator=True)
async def reset_raffle(interaction: discord.Interaction):
    # 1. Safety Check (Prevent accidental resets)
    await interaction.response.send_message(
        "⚠️ **WARNING: STARTING NEW RAFFLE** ⚠️\n"
        "This will set all ticket counts to **0**.\n"
        "It will NOT delete user links.\n\n"
        "Type `CONFIRM` in this channel to proceed."
    )

    def check(m):
        return m.author == interaction.user and m.content == "CONFIRM" and m.channel == interaction.channel

    try:
        # Wait 30 seconds for the user to type CONFIRM
        await bot.wait_for("message", check=check, timeout=30.0)
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ Timed out. Raffle NOT reset.")
        return

    # 2. Reset being done here
    raffle_data["tickets"] = {}
    raffle_data["meta"]["total_pool_value"] = 0
    
    # Save immediately
    save_json(RAFFLE_FILE, raffle_data)
    
    await interaction.followup.send(
        "✅ **Raffle Reset Complete!**\n"
        "• Ticket counts flushed to 0.\n"
        "• Pot value reset to $0.\n"
        "• Bot is ready to accept NEW items for the next round."
    )


bot.run(DISCORD_TOKEN)

