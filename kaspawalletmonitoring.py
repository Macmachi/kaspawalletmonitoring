# -*- coding: utf-8 -*-
# Auteur : Rymentz
# v1.0.0

import nest_asyncio
nest_asyncio.apply()
import asyncio
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
import re
from datetime import datetime, timezone

import aiohttp
import aiosqlite
import configparser  # For reading the configuration file

# Telegram and APScheduler imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import Forbidden
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- READING THE CONFIG FILE ---
config = configparser.ConfigParser()
config.read("config.ini")

BOT_TOKEN = config.get("telegram", "BOT_TOKEN")
KASPA_API_URL = config.get("kaspa", "KASPA_API_URL")
DONATION_ADDRESS = config.get("kaspa", "DONATION_ADDRESS")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global variable for the database connection
db_conn = None

# --- INITIALIZING THE DATABASE ---
async def init_db():
    """
    Initializes the aiosqlite database and creates the necessary tables:
      - chats: Chat ID, registration date, and total number of messages sent.
      - kaspa_addresses: Kaspa addresses monitored per chat with addition date and known transaction count.
      - daily_messages: Number of messages sent per chat per date.
      - command_usage: Command usage per chat.
      - balance_history: Balance history for each address (in Kas).
      - donations: Records donations with sender/recipient, amount, etc.
    """
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
            balance REAL,
            FOREIGN KEY(chat_id) REFERENCES chats(chat_id)
        )
        """
    )
    # Table to record donation transactions
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

# --- STATISTICS FUNCTIONS ---
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

# --- FUNCTION TO RECORD A DONATION TRANSACTION ---
async def record_donation_transaction(transaction_id: str, sender_address: str, recipient_address: str, amount: float):
    """
    Records a donation transaction in the donations table.
    Depending on the direction of the transaction (sent or received),
    the non-DONATION_ADDRESS field will contain either the sender's address (donation received)
    or the recipient's address (donation sent).
    """
    record_date = datetime.now(timezone.utc).isoformat()
    await db_conn.execute(
        "INSERT INTO donations (transaction_id, sender_address, recipient_address, amount, record_date) VALUES (?,?,?,?,?)",
        (transaction_id, sender_address, recipient_address, amount, record_date)
    )
    await db_conn.commit()

# --- FUNCTION TO REMOVE A CHAT ---
async def remove_chat(chat_id: int):
    """
    Deletes all entries (chats, kaspa_addresses, daily_messages, command_usage, balance_history)
    associated with a given chat.
    """
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

# --- BOT COMMANDS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start
    Registers the chat if not already registered and shows the list of available commands.
    """
    chat_id = update.effective_chat.id
    async with db_conn.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
    if row:
        # Chat is already registered, so log the command and send an updated message.
        await record_message(chat_id, "start")
        start_message = (
            "Hey, the bot is already running in this chat! 😊\n\n"
            "Available commands:\n"
            "🚀 /start - Restart the bot (message already displayed)\n"
            "➕ /addaddress <kaspa_address> - Add a Kaspa address to monitor\n"
            "📜 /myaddresses - View your monitored addresses\n"
            "💰 /donation - Donation information\n"
            "❓ /help - Help\n"
        )
        await update.message.reply_text(start_message)
        return
    else:
        # Initial registration
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
            "➕ /addaddress <kaspa_address> - Add a Kaspa address to monitor\n"
            "📜 /myaddresses - View your monitored addresses (with delete option)\n"
            "💰 /donation - Donation information\n"
            "❓ /help - Help\n"
        )
        await update.message.reply_text(start_message)

async def add_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /addaddress <kaspa_address>
    - Validates the address format.
    - Uses the Kaspa API to ensure that it has completed at least one transaction.
    - Registers the address for the chat if it does not already exist and creates an initial record in the history.
    """
    chat_id = update.effective_chat.id
    await record_message(chat_id, "addaddress")
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /addaddress <kaspa_address> 😇")
        return

    address = args[0].strip()
    if not re.fullmatch(r"^kaspa:[a-z0-9]{61,63}$", address):
        await update.message.reply_text("The Kaspa address format is incorrect. 😕")
        return

    # Call the transactions count endpoint asynchronously.
    count_url = f"{KASPA_API_URL}/addresses/{address}/transactions-count"
    headers = {"Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(count_url, timeout=10) as response:
                if response.status != 200:
                    await update.message.reply_text("Error verifying the address (API error code). 😢")
                    return
                data = await response.json()
    except Exception as e:
        logger.error("Error calling the Kaspa API for transactions count: %s", e)
        await update.message.reply_text("Error verifying the address. 😢")
        return

    if data.get("limit_exceeded", False):
        logger.warning("Transaction count limit exceeded for address %s", address)

    if not data.get("total", 0):
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
        (chat_id, address, added_date, 0)
    )
    await db_conn.commit()

    # Retrieve the initial balance and store it in the history.
    async with aiohttp.ClientSession(headers=headers) as session:
        balance_url = f"{KASPA_API_URL}/addresses/{address}/balance"
        try:
            async with session.get(balance_url, timeout=10) as response:
                if response.status != 200:
                    await update.message.reply_text("Error retrieving the balance for the address. 😢")
                    return
                data_balance = await response.json()
                balance = data_balance.get("balance", 0)
                kas_value = balance / 100_000_000
        except Exception as e:
            logger.error("Error retrieving balance for %s: %s", address, e)
            await update.message.reply_text("Error retrieving the balance for the address. 😢")
            return

    current_time = datetime.now(timezone.utc).isoformat()
    await db_conn.execute(
        "INSERT INTO balance_history (chat_id, address, record_date, balance) VALUES (?,?,?,?)",
        (chat_id, address, current_time, kas_value)
    )
    await db_conn.commit()

    await update.message.reply_text(f"Awesome! 😄 Address {address} registered successfully!")

async def myaddresses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /myaddresses
    Displays a list of addresses monitored by the user with a "Delete" button for each.
    """
    chat_id = update.effective_chat.id

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
        # Display a delete button with a trash can icon (using the last 6 characters of the address)
        keyboard.append([InlineKeyboardButton(f"🗑 Delete {address[-6:]}", callback_data=f"delete_address:{record_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(reply_text, reply_markup=reply_markup)

async def delete_address_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Callback for deleting an address via the inline "Delete" button.
    Ensures that the entry belongs to the user.
    """
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
        "SELECT id FROM kaspa_addresses WHERE id = ? AND chat_id = ?",
        (record_id, chat_id)
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        await query.edit_message_text("Sorry, you cannot delete this address. 😕")
        return

    await db_conn.execute(
        "DELETE FROM kaspa_addresses WHERE id = ? AND chat_id = ?",
        (record_id, chat_id)
    )
    await db_conn.commit()
    await query.edit_message_text("Address deleted successfully! 👍")

async def donation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /donation
    Displays the donation address and the total Kas received (converted by dividing by 100,000,000).
    """
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        url = f"{KASPA_API_URL}/addresses/{DONATION_ADDRESS}/balance"
        try:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    await update.message.reply_text("Oops, error retrieving donation data (API). 😢")
                    return
                data = await response.json()
        except Exception as e:
            logger.error("Error calling the Kaspa API for donations: %s", e)
            await update.message.reply_text("Error retrieving donation data. 😢")
            return

    balance = data.get("balance", 0)
    kas_value = balance / 100_000_000
    donation_message = (
        f"Donation address: {DONATION_ADDRESS}\n"
        f"Total Kas received: {kas_value} Kas 😊"
    )
    await update.message.reply_text(donation_message)

async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /help
    Displays instructions for using the bot.
    """
    help_text = (
        "Hello! 👋 Welcome to the Kaspa Wallet Monitor Help. Here are the available commands:\n\n"
        "/start - Register your chat and initialize your monitoring 😊\n"
        "/addaddress <kaspa_address> - Add a Kaspa address to monitor (it must have completed at least one transaction) ➕\n"
        "/myaddresses - List your monitored addresses with the option to delete each 🗑\n"
        "/donation - Show the donation address and total Kas received 💰\n"
        "/help - Display this help message ❓\n\n"
        "The bot automatically checks every minute for each monitored address. "
        "Alert messages will only be sent when the recorded balance actually changes! 🔄"
    )
    await update.message.reply_text(help_text)

# --- CHECK ADDRESSES FUNCTION ---
async def check_addresses(bot) -> None:
    """
    Checks every minute for each monitored address for new transactions.
    If a new transaction is detected, it updates the transaction counter, records the new balance, and sends an alert message
    only if the balance changed.
    In case of an error (e.g., bot is blocked or chat deleted), the chat is removed from the database.
    """
    async with db_conn.execute("SELECT id, chat_id, address, last_tx_count FROM kaspa_addresses") as cursor:
        rows = await cursor.fetchall()
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for record_id, chat_id, address, last_tx in rows:
            tx_count_url = f"{KASPA_API_URL}/addresses/{address}/transactions-count"
            try:
                async with session.get(tx_count_url, timeout=10) as response:
                    if response.status != 200:
                        continue
                    data = await response.json()
                    new_tx_count = data.get("total", 0)
            except Exception as e:
                logger.error("Error checking address %s: %s", address, e)
                continue

            if new_tx_count > last_tx:
                await db_conn.execute(
                    "UPDATE kaspa_addresses SET last_tx_count = ? WHERE id = ?",
                    (new_tx_count, record_id)
                )
                await db_conn.commit()

                async with db_conn.execute(
                    "SELECT balance FROM balance_history WHERE chat_id = ? AND address = ? ORDER BY record_date DESC LIMIT 1",
                    (chat_id, address)
                ) as cursor2:
                    res = await cursor2.fetchone()
                old_balance = res[0] if res else 0

                balance_url = f"{KASPA_API_URL}/addresses/{address}/balance"
                try:
                    async with session.get(balance_url, timeout=10) as response:
                        if response.status != 200:
                            continue
                        data_balance = await response.json()
                        balance = data_balance.get("balance", 0)
                        kas_value = balance / 100_000_000
                except Exception as e:
                    logger.error("Error retrieving balance for %s: %s", address, e)
                    continue

                if kas_value == old_balance:
                    logger.info("Transaction detected for address %s but balance hasn't changed.", address)
                    continue

                if old_balance > 0:
                    difference = kas_value - old_balance
                    percentage_change = (difference / old_balance) * 100
                else:
                    difference = kas_value
                    percentage_change = 100

                direction = "📥 Received" if difference > 0 else "📤 Sent"
                target_info = ""
                tx_page_url = f"{KASPA_API_URL}/addresses/{address}/full-transactions-page?limit=1"
                try:
                    async with session.get(tx_page_url, timeout=10) as tx_response:
                        if tx_response.status == 200:
                            tx_data = await tx_response.json()
                            if tx_data and isinstance(tx_data, list) and len(tx_data) > 0:
                                tx = tx_data[0]
                                if difference > 0:
                                    # For an incoming transaction, extract the sender address.
                                    sender = None
                                    for inp in tx.get("inputs", []):
                                        candidate = inp.get("previous_outpoint_address")
                                        if not candidate and inp.get("previous_outpoint_resolved"):
                                            candidate = inp["previous_outpoint_resolved"].get("script_public_key_address")
                                        if candidate and candidate != address:
                                            sender = candidate
                                            break
                                    target_info = f"\nFrom: {sender}" if sender else ""
                                else:
                                    # For an outgoing transaction, extract the recipient address.
                                    recipient = None
                                    for out in tx.get("outputs", []):
                                        candidate = out.get("script_public_key_address")
                                        if candidate and candidate != address:
                                            recipient = candidate
                                            break
                                    target_info = f"\nTo: {recipient}" if recipient else ""
                            else:
                                target_info = ""
                        else:
                            target_info = ""
                except Exception as e:
                    logger.error("Error retrieving transaction details for %s: %s", address, e)
                    target_info = ""

                current_time = datetime.now(timezone.utc).isoformat()
                await db_conn.execute(
                    "INSERT INTO balance_history (chat_id, address, record_date, balance) VALUES (?,?,?,?)",
                    (chat_id, address, current_time, kas_value)
                )
                await db_conn.commit()

                alert_message = (
                    f"🎉 New transaction detected for address "
                    f"<a href='https://explorer.kaspa.org/addresses/{address}?page=1'>{address}</a>! 😊\n"
                    f"🔴 Previous balance: {old_balance} Kas\n"
                    f"🟢 New balance: {kas_value} Kas\n"
                    f"🔄 Change: {percentage_change:.2f}% compared to the previous balance.\n"
                    f"💸 Amount {direction}: {abs(difference):.2f} Kas"
                    f"{target_info}"
                )
                try:
                    await bot.send_message(chat_id=chat_id, text=alert_message, parse_mode='HTML')
                except Forbidden as e:
                    logger.error("Bot blocked or chat deleted for chat %s when sending alert: %s", chat_id, e)
                    await remove_chat(chat_id)
                except Exception as e:
                    logger.error("Error sending alert for %s to chat %s: %s", address, chat_id, e)

# --- MONTHLY DONATION / DONATOR SUMMARY FUNCTION ---
async def send_monthly_donation_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    This function is called every month.
    It retrieves the donation records in the current month (ignoring previous months) and constructs a message listing:
      - For a transaction where the recipient address is DONATION_ADDRESS:
            "Donation received from <sender_address> on <date>: <amount> Kas"
      - For a transaction where the sender address is DONATION_ADDRESS:
            "Donation sent to <recipient_address> on <date>: <amount> Kas"
    If no donation is recorded for the month, the default message is:
            "No donations recorded this month. Let's keep supporting Kaspa! 😔"
    This message is sent to all registered chats.
    """
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
        donation_message = "No donations recorded this month. Let's keep supporting Kaspa! 😔"

    for (chat_id,) in chat_rows:
        try:
            await context.bot.send_message(chat_id=chat_id, text=donation_message)
        except Forbidden as e:
            logger.error("Bot blocked or chat deleted for chat %s when sending monthly donation summary: %s", chat_id, e)
            await remove_chat(chat_id)
        except Exception as e:
            logger.error("Error sending monthly donation summary to chat %s: %s", chat_id, e)

# --- MAIN ---
async def main() -> None:
    global db_conn
    db_conn = await init_db()
    
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("addaddress", add_address))
    application.add_handler(CommandHandler("donation", donation))
    application.add_handler(CommandHandler("myaddresses", myaddresses))
    application.add_handler(CommandHandler("help", aide))
    application.add_handler(CallbackQueryHandler(delete_address_callback, pattern=r"^delete_address:\d+$"))

    scheduler = AsyncIOScheduler(timezone="UTC")
    # Monthly donation/donators summary job. Every 1st of the month at 00:00.
    scheduler.add_job(send_monthly_donation_message, "cron", day=1, hour=0, minute=0, args=[application.bot])
    # Check monitored addresses every minute for new transactions.
    scheduler.add_job(check_addresses, "interval", seconds=60, args=[application.bot])
    scheduler.start()

    logger.info("Bot started...")
    await application.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")