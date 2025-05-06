# -*- coding: utf-8 -*-
# Auteur : Rymentz
# v1.0.6

import nest_asyncio
nest_asyncio.apply()
import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
logging.getLogger("httpx").setLevel(logging.WARNING)
import re
from datetime import datetime, timezone
import aiohttp
import aiosqlite
import configparser
import os
import sys

# Telegram and APScheduler imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import Forbidden
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- READING THE CONFIG FILE ---
config = configparser.ConfigParser()
# Make sure config.ini is in the same directory or specify the full path
config_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
if not os.path.exists(config_file_path):
    print(f"Erreur: Le fichier de configuration '{config_file_path}' est introuvable.")
    sys.exit(1)
config.read(config_file_path)

BOT_TOKEN = config.get("telegram", "BOT_TOKEN")
KASPA_API_URL = config.get("kaspa", "KASPA_API_URL") # e.g., https://api.kaspa.org
DONATION_ADDRESS = config.get("kaspa", "DONATION_ADDRESS")
CMC_API_KEY = config.get("coinmarketcap", "API_KEY")
KASPA_CMC_ID = config.get("coinmarketcap", "KASPA_ID")

log_dir = "logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

log_file = os.path.join(log_dir, "bot.log")
file_handler = TimedRotatingFileHandler(
    log_file,
    when="W0",
    interval=1,
    backupCount=6
)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    # Adding StreamHandler for console output
    handlers=[file_handler, logging.StreamHandler(sys.stdout)] 
)

logger = logging.getLogger(__name__)

class LoggerWriter:
    def __init__(self, logger_instance, level):
        self.logger_instance = logger_instance
        self.level = level
    def write(self, message):
        message = message.strip()
        if message:
            self.logger_instance.log(self.level, message)
    def flush(self):
        pass

sys.stdout = LoggerWriter(logger, logging.INFO)
sys.stderr = LoggerWriter(logger, logging.ERROR)

db_conn = None
current_kaspa_price = 0.0

# --- HELPER FUNCTION: fetch_with_retry (for GET requests) ---
async def fetch_with_retry(session: aiohttp.ClientSession, url: str, timeout: int = 10, retries: int = 3, is_json=True):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json() if is_json else await response.text()
                else:
                    logger.error(f"API GET call to {url} returned status {response.status}. Response: {await response.text()}")
                    return None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on GET attempt {attempt + 1} for url: {url}")
            await asyncio.sleep(2 ** attempt)
        except aiohttp.ClientError as e: # More specific for network errors
            logger.error(f"ClientError fetching data from {url} on attempt {attempt + 1}: {e}")
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Generic error fetching data from {url} on attempt {attempt + 1}: {e}")
            break # Break on unknown errors
    return None

# --- HELPER FUNCTION: post_with_retry (for POST requests) ---
async def post_with_retry(session: aiohttp.ClientSession, url: str, payload: dict, timeout: int = 10, retries: int = 3, is_json=True):
    for attempt in range(retries):
        try:
            async with session.post(url, json=payload, timeout=timeout) as response:
                if response.status == 200:
                    return await response.json() if is_json else await response.text()
                else:
                    logger.error(f"API POST call to {url} with payload {payload} returned status {response.status}. Response: {await response.text()}")
                    return None
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on POST attempt {attempt + 1} for url: {url}")
            await asyncio.sleep(2 ** attempt)
        except aiohttp.ClientError as e:
            logger.error(f"ClientError posting data to {url} on attempt {attempt + 1}: {e}")
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"Generic error posting data to {url} on attempt {attempt + 1}: {e}")
            break
    return None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception occurred: %s", context.error, exc_info=context.error)
    if update and hasattr(update, "effective_message") and update.effective_message:
        try:
            await update.effective_message.reply_text("An unexpected error occurred. Please try again later.")
        except Exception as e:
            logger.error("Failed to send error message: %s", e)

async def init_db():
    conn = await aiosqlite.connect("bot_database.db")
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            start_date TEXT,
            messages_count INTEGER DEFAULT 0
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kaspa_addresses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            address TEXT,
            added_date TEXT,
            last_tx_count INTEGER DEFAULT 0,
            FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_messages (
            chat_id INTEGER,
            date TEXT,
            message_count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, date)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_usage (
            chat_id INTEGER,
            command TEXT,
            usage_count INTEGER DEFAULT 0,
            last_used_date TEXT,
            PRIMARY KEY (chat_id, command)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS balance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            address TEXT,
            record_date TEXT,
            balance REAL, -- Stored in Kas
            FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT,
            sender_address TEXT,
            recipient_address TEXT,
            amount REAL,
            record_date TEXT
        )
        """
    )
    await conn.commit()
    return conn

async def increment_message_count(chat_id: int):
    try:
        await db_conn.execute(
            "UPDATE chats SET messages_count = messages_count + 1 WHERE chat_id = ?",
            (chat_id,)
        )
        await db_conn.commit()
    except Exception as e:
        logger.error("Error while incrementing global message counter: %s", e)

async def increment_daily_message_count(chat_id: int):
    try:
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with db_conn.execute(
            "SELECT message_count FROM daily_messages WHERE chat_id = ? AND date = ?",
            (chat_id, current_date)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db_conn.execute(
                "UPDATE daily_messages SET message_count = message_count + 1 WHERE chat_id = ? AND date = ?",
                (chat_id, current_date)
            )
        else:
            await db_conn.execute(
                "INSERT INTO daily_messages (chat_id, date, message_count) VALUES (?,?,?)",
                (chat_id, current_date, 1)
            )
        await db_conn.commit()
    except Exception as e:
        logger.error("Error while incrementing daily message count: %s", e)

async def update_command_usage(chat_id: int, command: str):
    try:
        current_date = datetime.now(timezone.utc).isoformat()
        async with db_conn.execute(
            "SELECT usage_count FROM command_usage WHERE chat_id = ? AND command = ?",
            (chat_id, command)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db_conn.execute(
                "UPDATE command_usage SET usage_count = usage_count + 1, last_used_date = ? WHERE chat_id = ? AND command = ?",
                (current_date, chat_id, command)
            )
        else:
            await db_conn.execute(
                "INSERT INTO command_usage (chat_id, command, usage_count, last_used_date) VALUES (?,?,?,?)",
                (chat_id, command, 1, current_date)
            )
        await db_conn.commit()
    except Exception as e:
        logger.error("Error while updating command usage for '%s': %s", command, e)

async def record_message(chat_id: int, command: str = None):
    await increment_message_count(chat_id)
    await increment_daily_message_count(chat_id)
    if command:
        await update_command_usage(chat_id, command)

async def record_donation_transaction(transaction_id: str, sender_address: str, recipient_address: str, amount: float):
    record_date = datetime.now(timezone.utc).isoformat()
    await db_conn.execute(
        "INSERT INTO donations (transaction_id, sender_address, recipient_address, amount, record_date) VALUES (?,?,?,?,?)",
        (transaction_id, sender_address, recipient_address, amount, record_date)
    )
    await db_conn.commit()

async def remove_chat(chat_id: int):
    try:
        await db_conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        await db_conn.execute("DELETE FROM kaspa_addresses WHERE chat_id = ?", (chat_id,))
        await db_conn.execute("DELETE FROM daily_messages WHERE chat_id = ?", (chat_id,))
        await db_conn.execute("DELETE FROM command_usage WHERE chat_id = ?", (chat_id,))
        await db_conn.execute("DELETE FROM balance_history WHERE chat_id = ?", (chat_id,))
        await db_conn.commit()
        logger.info("Chat %s removed from the database (bot blocked or chat deleted).", chat_id)
    except Exception as e:
        logger.error("Error removing chat %s: %s", chat_id, e)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with db_conn.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
    if row:
        await record_message(chat_id, "start")
        start_message = (
            "Hey, the bot is already running in this chat! 😊\n\n"
            "Available commands:\n"
            "🚀 /start - Restart the bot (message already displayed)\n"
            "➕ /wallet <kaspa_address> - Add a Kaspa address to monitor\n"
            "📜 /myaddresses - View your monitored addresses\n"
            "💰 /donation - Donation information\n"
            "❓ /help - Help\n"
        )
        await update.message.reply_text(start_message)
        return
    else:
        start_date = datetime.now(timezone.utc).isoformat()
        await db_conn.execute(
            "INSERT INTO chats (chat_id, start_date, messages_count) VALUES (?,?,?)",
            (chat_id, start_date, 0)
        )
        await db_conn.commit()
        await record_message(chat_id, "start")
        start_message = (
            "Welcome! 😃 You are now registered to monitor Kaspa addresses.\n\n"
            "Available commands:\n"
            "🚀 /start - Start or restart the bot\n"
            "➕ /wallet <kaspa_address> - Add a Kaspa address to monitor\n"
            "📜 /myaddresses - View your monitored addresses (with delete option)\n"
            "💰 /donation - Donation information\n"
            "❓ /help - Help\n"
        )
        await update.message.reply_text(start_message)

async def add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await record_message(chat_id, "wallet")
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /wallet <kaspa_address> 😇")
        return

    address = args[0].strip()
    if not re.fullmatch(r"^kaspa:[a-z0-9]{61,63}$", address):
        await update.message.reply_text("The Kaspa address format is incorrect. 😕")
        return

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        # Check if address has transactions
        count_url = f"{KASPA_API_URL}/addresses/{address}/transactions-count"
        data_count = await fetch_with_retry(session, count_url)
        if data_count is None:
            await update.message.reply_text("Error verifying the address (API timeout or error for tx count). 😢")
            return
        if data_count.get("limit_exceeded", False):
            logger.warning("Transaction count limit exceeded for address %s", address)
        if not data_count.get("total", 0):
            await update.message.reply_text("This address hasn't made any transactions yet. 😕")
            return

        async with db_conn.execute(
            "SELECT id FROM kaspa_addresses WHERE chat_id = ? AND address = ?",
            (chat_id, address)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await update.message.reply_text("This address is already registered! 😉")
            return

        added_date = datetime.now(timezone.utc).isoformat()
        await db_conn.execute(
            "INSERT INTO kaspa_addresses (chat_id, address, added_date, last_tx_count) VALUES (?,?,?,?)",
            (chat_id, address, added_date, 0) # Initial last_tx_count is 0
        )
        await db_conn.commit()

        # Retrieve initial balance using the new /addresses/balances endpoint
        balance_payload = {"addresses": [address]}
        balance_api_endpoint = f"{KASPA_API_URL}/addresses/balances"
        balance_data_list = await post_with_retry(session, balance_api_endpoint, payload=balance_payload)

        kas_value = 0
        if balance_data_list and isinstance(balance_data_list, list) and len(balance_data_list) > 0:
            balance_info = balance_data_list[0]
            if balance_info.get("address") == address:
                balance_sompi = balance_info.get("balance", 0)
                kas_value = balance_sompi / 100_000_000
            else:
                await update.message.reply_text("Error: API returned balance for a different address. 😢")
                return
        else:
            await update.message.reply_text("Error retrieving the balance for the address. 😢")
            # Potentially rollback the address insertion or handle this state
            return

    current_time = datetime.now(timezone.utc).isoformat()
    await db_conn.execute(
        "INSERT INTO balance_history (chat_id, address, record_date, balance) VALUES (?,?,?,?)",
        (chat_id, address, current_time, kas_value)
    )
    await db_conn.commit()

    await update.message.reply_text(f"Awesome! 😄 Address {address} registered successfully with balance {kas_value:.8f} KAS!")


async def myaddresses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await record_message(chat_id, "myaddresses")
    async with db_conn.execute(
        "SELECT id, address, added_date FROM kaspa_addresses WHERE chat_id = ?",
        (chat_id,)
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        await update.message.reply_text("You haven't registered any addresses yet. 😊")
        return

    reply_text = "Here are your registered addresses:\n\n"
    keyboard = []
    for record_id, address, added_date in rows:
        date_only = added_date.split("T")[0]
        reply_text += f"• {address} (added on {date_only})\n"
        keyboard.append([InlineKeyboardButton(f"🗑 Delete {address[-6:]}", callback_data=f"delete_address:{record_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(reply_text, reply_markup=reply_markup)

async def delete_address_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if not data.startswith("delete_address:"):
        return
    try:
        record_id = int(data.split(":", 1)[1])
    except ValueError:
        await query.edit_message_text("Invalid data. 😕")
        return

    chat_id = update.effective_chat.id
    async with db_conn.execute(
        "SELECT address FROM kaspa_addresses WHERE id = ? AND chat_id = ?", # Get address for logging
        (record_id, chat_id)
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        await query.edit_message_text("Sorry, you cannot delete this address. 😕")
        return
    
    address_to_delete = row[0]

    await db_conn.execute(
        "DELETE FROM kaspa_addresses WHERE id = ? AND chat_id = ?",
        (record_id, chat_id)
    )
    # Also delete from balance_history
    await db_conn.execute(
        "DELETE FROM balance_history WHERE address = ? AND chat_id = ?",
        (address_to_delete, chat_id)
    )
    await db_conn.commit()
    await query.edit_message_text(f"Address {address_to_delete} deleted successfully! 👍")


async def donation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await record_message(chat_id, "donation")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    balance_payload = {"addresses": [DONATION_ADDRESS]}
    balance_api_endpoint = f"{KASPA_API_URL}/addresses/balances"
    
    async with aiohttp.ClientSession(headers=headers) as session:
        balance_data_list = await post_with_retry(session, balance_api_endpoint, payload=balance_payload)

        if balance_data_list and isinstance(balance_data_list, list) and len(balance_data_list) > 0:
            balance_info = balance_data_list[0]
            if balance_info.get("address") == DONATION_ADDRESS:
                balance_sompi = balance_info.get("balance", 0)
                kas_value = balance_sompi / 100_000_000
                smiley = "😊" if kas_value > 0 else "😕"
                donation_message = (
                    f"Donation address: {DONATION_ADDRESS}\n"
                    f"Total Kas received: {kas_value:.2f} Kas {smiley}"
                )
                await update.message.reply_text(donation_message)
            else:
                await update.message.reply_text("Oops, error retrieving donation data (API address mismatch). 😢")
        else:
            await update.message.reply_text("Oops, error retrieving donation data (API). 😢")

async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await record_message(chat_id, "help")
    help_text = (
        "Hello! 👋 Welcome to the Kaspa Wallet Monitor Help. Here are the available commands:\n\n"
        "/start - Register your chat and initialize your monitoring 😊\n"
        "/wallet <kaspa_address> - Add a Kaspa address to monitor (it must have completed at least one transaction) ➕\n"
        "/myaddresses - List your monitored addresses with the option to delete each 🗑\n"
        "/donation - Show the donation address and total Kas received 💰\n"
        "/help - Display this help message ❓\n\n"
        "The bot automatically checks every minute for each monitored address. "
        "Alert messages will only be sent when the recorded balance actually changes! 🔄"
    )
    await update.message.reply_text(help_text)

# --- OPTIMIZED CHECK ADDRESSES FUNCTION ---
async def check_addresses(bot) -> None:
    # 1. Group addresses by their value to avoid duplicates
    address_to_chats = {}  # Maps addresses to list of (chat_id, record_id, last_tx_count)
    
    async with db_conn.execute("SELECT id, chat_id, address, last_tx_count FROM kaspa_addresses") as cursor:
        rows = await cursor.fetchall()
    
    for record_id, chat_id, address, last_tx_count in rows:
        if address not in address_to_chats:
            address_to_chats[address] = []
        address_to_chats[address].append((chat_id, record_id, last_tx_count))
    
    if not address_to_chats:  # No addresses to check
        return
    
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    
    async with aiohttp.ClientSession(headers=headers) as session:
        # 2. Check each unique address only once
        for address, chat_records in address_to_chats.items():
            # Get current balance from API
            balance_payload = {"addresses": [address]}
            balance_api_endpoint = f"{KASPA_API_URL}/addresses/balances"
            api_balance_kas = 0
            valid_balance_fetched = False

            balance_data_list = await post_with_retry(session, balance_api_endpoint, payload=balance_payload)
            if balance_data_list and isinstance(balance_data_list, list) and len(balance_data_list) > 0:
                balance_info = balance_data_list[0]
                if balance_info.get("address") == address:
                    balance_sompi = balance_info.get("balance", 0)
                    api_balance_kas = balance_sompi / 100_000_000
                    valid_balance_fetched = True
                else:
                    logger.warning(f"Balance API returned data for wrong address. Expected {address}, got {balance_info.get('address')}")
            else:
                logger.error(f"Failed to fetch balance for {address} or empty/invalid response: {balance_data_list}")
            
            if not valid_balance_fetched:
                continue  # Skip this address if balance couldn't be fetched

            # Get transaction count (once per address)
            tx_count_url = f"{KASPA_API_URL}/addresses/{address}/transactions-count"
            tx_count_data = await fetch_with_retry(session, tx_count_url)
            new_api_tx_count = 0  # Default
            if tx_count_data and tx_count_data.get("total") is not None:
                new_api_tx_count = tx_count_data.get("total", 0)
            
            # Track if we need to fetch transaction details
            any_balance_changed = False
            chats_with_balance_change = []
            
            # Process each chat that monitors this address
            for chat_id, record_id, last_tx_db_count in chat_records:
                # Get previous balance from DB history
                async with db_conn.execute(
                    "SELECT balance FROM balance_history WHERE chat_id = ? AND address = ? ORDER BY record_date DESC LIMIT 1",
                    (chat_id, address)
                ) as cursor_prev_balance:
                    res_prev_balance = await cursor_prev_balance.fetchone()
                
                db_old_balance_kas = res_prev_balance[0] if res_prev_balance else 0  # Default to 0 if no history
                
                # Check if balance has changed significantly
                balance_changed = abs(api_balance_kas - db_old_balance_kas) > 1e-9
                
                # Update balance_history regardless of change
                current_time_iso = datetime.now(timezone.utc).isoformat()
                await db_conn.execute(
                    "INSERT INTO balance_history (chat_id, address, record_date, balance) VALUES (?,?,?,?)",
                    (chat_id, address, current_time_iso, api_balance_kas)
                )
                
                # Update last_tx_count in DB if it changed
                if new_api_tx_count != last_tx_db_count:
                    await db_conn.execute(
                        "UPDATE kaspa_addresses SET last_tx_count = ? WHERE id = ?",
                        (new_api_tx_count, record_id)
                    )
                
                # If balance changed, add this chat to our list for alerts
                if balance_changed:
                    any_balance_changed = True
                    chats_with_balance_change.append((chat_id, db_old_balance_kas))
            
            # Commit all database updates for this address
            await db_conn.commit()
            
            # If no balance changes for any chat, skip alert processing
            if not any_balance_changed:
                continue
            
            # Only fetch transaction details if there's at least one chat with a balance change
            tx_details = None
            tx_link_explorer = f"https://explorer.kaspa.org/addresses/{address}"
            
            # Fetch the latest transaction details for the alerts
            latest_tx_url = f"{KASPA_API_URL}/addresses/{address}/full-transactions?limit=1&offset=0&resolve_previous_outpoints=full"
            latest_tx_data_list = await fetch_with_retry(session, latest_tx_url)
            
            if latest_tx_data_list and isinstance(latest_tx_data_list, list) and len(latest_tx_data_list) > 0:
                tx_details = latest_tx_data_list[0]
                tx_id = tx_details.get("transaction_id")
                if tx_id:
                    tx_link_explorer = f"https://explorer.kaspa.org/txs/{tx_id}"
            
            # Send alerts to each chat with a balance change
            for chat_id, db_old_balance_kas in chats_with_balance_change:
                difference_kas = api_balance_kas - db_old_balance_kas
                alert_message_text = ""
                
                if tx_details:
                    subnetwork_id = tx_details.get("subnetwork_id", "00")
                    is_accepted = tx_details.get("is_accepted", False)
                    
                    usd_amount = abs(difference_kas) * current_kaspa_price
                    
                    if subnetwork_id.startswith("01"):  # Mining transaction
                        if is_accepted:
                            alert_message_text = (
                                f"⛏️ Mining Reward Accepted for: <a href='{tx_link_explorer}'>{address}</a>\n"
                                f"💰 Amount: +{abs(difference_kas):.2f} Kas (~${usd_amount:.2f} USD)\n"
                                f"🔴 Previous balance: {db_old_balance_kas:.2f} Kas\n"
                                f"🟢 New balance: {api_balance_kas:.2f} Kas"
                            )
                        else:
                            logger.info(f"Mining transaction {tx_id or 'N/A'} for {address} is not accepted. No alert sent.")
                            continue  # Skip alert for not-accepted mining transactions
                    else:  # Regular transaction
                        direction = "📥 Received" if difference_kas > 0 else "📤 Sent"
                        percentage_change_info = ""
                        if abs(db_old_balance_kas) > 1e-9:
                            percentage_change = (difference_kas / db_old_balance_kas) * 100
                            percentage_change_info = f"\n🔄 Change: {percentage_change:+.2f}%"
                        else:
                            percentage_change_info = "\n🔄 New funds received!" if difference_kas > 0 else ""
                        
                        target_info = ""
                        # Simplified target info (first non-self input/output)
                        if difference_kas > 0:  # Received
                            sender = "Unknown Sender"
                            if tx_details.get("inputs"):
                                for inp in tx_details["inputs"]:
                                    prev_addr = inp.get("previous_outpoint_address")
                                    if prev_addr and prev_addr != address:
                                        sender = prev_addr
                                        break
                            target_info = f"\nFrom: {sender}"
                        else:  # Sent
                            recipient = "Unknown Recipient"
                            if tx_details.get("outputs"):
                                for out in tx_details["outputs"]:
                                    out_addr = out.get("script_public_key_address")
                                    if out_addr and out_addr != address:
                                        recipient = out_addr
                                        break
                            target_info = f"\nTo: {recipient}"
                        
                        alert_message_text = (
                            f"🎉 Transaction detected for: <a href='{tx_link_explorer}'>{address}</a>\n"
                            f"🔴 Previous balance: {db_old_balance_kas:.2f} Kas\n"
                            f"🟢 New balance: {api_balance_kas:.2f} Kas"
                            f"{percentage_change_info}\n"
                            f"💸 Amount {direction}: {difference_kas:+.2f} Kas (~${usd_amount:.2f} USD)"
                            f"{target_info}"
                        )
                else:  # Fallback if transaction details couldn't be fetched
                    direction = "📥 Received" if difference_kas > 0 else "📤 Sent"
                    usd_amount = abs(difference_kas) * current_kaspa_price
                    alert_message_text = (
                        f"⚠️ Balance Change for: <a href='{tx_link_explorer}'>{address}</a>\n"
                        f"🔴 Previous balance: {db_old_balance_kas:.2f} Kas\n"
                        f"🟢 New balance: {api_balance_kas:.2f} Kas\n"
                        f"💸 Amount {direction}: {difference_kas:+.2f} Kas (~${usd_amount:.2f} USD)\n"
                        f"(Could not fetch specific transaction details for this alert)"
                    )
                
                if alert_message_text:
                    try:
                        # Check if the address is still being monitored by this chat
                        async with db_conn.execute(
                            "SELECT id FROM kaspa_addresses WHERE chat_id = ? AND address = ?",
                            (chat_id, address)
                        ) as cursor:
                            address_still_monitored = await cursor.fetchone()

                        if address_still_monitored:  # Only send the alert if the address is still being monitored
                            await bot.send_message(chat_id=chat_id, text=alert_message_text, parse_mode='HTML')
                            logger.info(f"Sent alert to chat {chat_id} for address {address}.")
                        else:
                            logger.info(f"Skipping alert for chat {chat_id} and address {address} as it's no longer monitored.")
                    except Forbidden:
                        logger.error(f"Bot blocked or chat deleted for chat {chat_id}. Removing chat.")
                        await remove_chat(chat_id)
                    except Exception as e:
                        logger.error(f"Error sending alert for {address} to chat {chat_id}: {e}")

async def send_monthly_donation_message(bot) -> None:
    start_of_month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    async with db_conn.execute(
        "SELECT sender_address, recipient_address, amount, record_date FROM donations WHERE record_date >= ?",
        (start_of_month,)
    ) as cursor:
        donation_records = await cursor.fetchall()

    async with db_conn.execute("SELECT chat_id FROM chats") as cursor:
        chat_rows = await cursor.fetchall()

    if donation_records:
        donation_message = "This month's donation summary:\n\n"
        for sender_address, recipient_address, amount, record_date in donation_records:
            date_only = record_date.split("T")[0]
            if recipient_address == DONATION_ADDRESS:
                donation_message += f"Donation received from {sender_address} on {date_only}: {amount} Kas\n"
            elif sender_address == DONATION_ADDRESS:
                donation_message += f"Donation sent to {recipient_address} on {date_only}: {amount} Kas\n"
    else:
        donation_message = "No donations recorded this month. Let's keep supporting Kaspa! 😔\n"

    donation_message += f"\nDonation address: {DONATION_ADDRESS}\n"
    donation_message += "If you like my bot, even 1 kaspa helps keep the service operational."

    for (chat_id,) in chat_rows:
        try:
            await bot.send_message(chat_id=chat_id, text=donation_message)
        except Forbidden:
            logger.error("Bot blocked or chat deleted for chat %s when sending monthly donation summary.", chat_id)
            await remove_chat(chat_id)
        except Exception as e:
            logger.error("Error sending monthly donation summary to chat %s: %s", chat_id, e)

async def update_kaspa_price():
    global current_kaspa_price
    headers = {
         "Accept": "application/json",
         "X-CMC_PRO_API_KEY": CMC_API_KEY
    }
    url = f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?id={KASPA_CMC_ID}&convert=USD"
    async with aiohttp.ClientSession(headers=headers) as session:
         data = await fetch_with_retry(session, url)
         if data and "data" in data and str(KASPA_CMC_ID) in data["data"]:
             try:
                 price = data["data"][str(KASPA_CMC_ID)]["quote"]["USD"]["price"]
                 current_kaspa_price = float(price)
                 logger.info("Kaspa price updated: %f USD", current_kaspa_price)
             except (KeyError, TypeError, ValueError) as e:
                 logger.error("Invalid data format or missing price from CoinMarketCap: %s. Error: %s", data, e)
         else:
             logger.error("Error fetching Kaspa price or unexpected response structure. Last known value: %f USD", current_kaspa_price)

async def main() -> None:
    global db_conn
    db_conn = await init_db()

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("wallet", add_address))
    application.add_handler(CommandHandler("donation", donation))
    application.add_handler(CommandHandler("myaddresses", myaddresses))
    application.add_handler(CommandHandler("help", aide))
    application.add_handler(CallbackQueryHandler(delete_address_callback, pattern=r"^delete_address:\d+$"))
    application.add_error_handler(error_handler)
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    await update_kaspa_price() # Initial price fetch
    scheduler.add_job(update_kaspa_price, "interval", minutes=15)
    scheduler.add_job(send_monthly_donation_message, "cron", day=1, hour=0, minute=0, args=[application.bot])
    scheduler.add_job(check_addresses, "interval", seconds=30, args=[application.bot])   
    scheduler.start()

    logger.info("Bot started...")
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
    finally:
        if db_conn:
            asyncio.run(db_conn.close()) # Ensure DB connection is closed