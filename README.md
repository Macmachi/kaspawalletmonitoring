#  Kaspa Wallet Monitoring (Telegram Bot)
This project implements a **Telegram bot** that monitors Kaspa addresses by interfacing with a dedicated API. The bot checks for new transactions and balance changes for each registered Kaspa address and provides notifications directly via Telegram. Users can also manage their monitored addresses with simple bot commands.

My bot is available on Telegram: @kaspawalletmonitor_bot

## Features

- **Database Initialization**  
  Uses `aiosqlite` to manage a SQLite database with several tables:
  - **chats**: Registers chats with their registration date and the total number of messages received.
  - **kaspa_addresses**: Stores monitored Kaspa addresses for each chat, along with their addition date and the last known transaction count.
  - **daily_messages**: Logs the number of messages per chat on a daily basis.
  - **command_usage**: Tracks command usage per chat.
  - **balance_history**: Keeps a history of each address’s balance (in Kas).
  - **donations**: Records donation transactions.

- **Bot Commands**  
  The bot provides several commands for an intuitive user experience:
  - `/start` – Registers the chat and displays the list of available commands.
  - `/addaddress <kaspa_address>` – Adds a Kaspa address to monitor after validating the address format and confirming it has at least one completed transaction.
  - `/myaddresses` – Lists the monitored addresses for the chat and offers a delete option for each.
  - `/donation` – Displays the donation address and shows the total amount of Kas received.
  - `/help` – Provides help on how to use the bot.

- **Real-Time Monitoring and Notifications**  
  - Uses `APScheduler` to schedule asynchronous tasks.
  - Checks monitored addresses every minute for any new transactions.
  - Sends an alert message when a balance change is detected, including details such as:
    - Previous and new balances.
    - The percentage change.
    - The amount sent or received.
    - Additional details such as sender or recipient information.

- **Monthly Donation Summary**  
  Once a month, the bot compiles and sends a summary message of donation transactions (both sent and received) to all registered chats.

## Prerequisites and Installation

### Prerequisites

- Python 3.12 or higher
- A Telegram API key (BOT_TOKEN) obtained from [BotFather](https://core.telegram.org/bots#botfather)
- Kaspa API URL and a donation address

### Installing Dependencies

Install the required dependencies using pip:

```bash
pip install nest_asyncio aiohttp aiosqlite python-telegram-bot apscheduler
```

### Configuration

Create a `config.ini` file in the project’s root directory to configure the necessary tokens and URLs:

```ini
[telegram]
BOT_TOKEN = your_bot_token_here

[kaspa]
KASPA_API_URL = https://api.kaspa.example
DONATION_ADDRESS = your_donation_address_here
```

## How to Use

To run the bot:

```bash
python your_script_name.py
```

Running the script initializes the database, starts the Telegram bot, and launches the scheduled tasks that check for new transactions and send periodic summaries.

## Database Structure

The bot uses a SQLite database (`bot_database.db`) with the following tables:

- **chats**  
  - `chat_id` (Primary Key)
  - `start_date`: Registration date of the chat
  - `messages_count`: Total messages received

- **kaspa_addresses**  
  - `id` (Primary Key, Auto Increment)
  - `chat_id`: ID of the chat registering the address
  - `address`: The Kaspa address being monitored
  - `added_date`: Date when the address was added
  - `last_tx_count`: Last known transaction count for the address

- **daily_messages**  
  - Composite Key of (`chat_id`, `date`)
  - `message_count`: Number of messages in a day

- **command_usage**  
  - Composite Key of (`chat_id`, `command`)
  - `usage_count`: Number of times the command was used
  - `last_used_date`: Date the command was last executed

- **balance_history**  
  - `id` (Primary Key, Auto Increment)
  - `chat_id`
  - `address`
  - `record_date`: Date of the recorded balance
  - `balance`: Balance in Kas

- **donations**  
  - `id` (Primary Key, Auto Increment)
  - `transaction_id`: Unique transaction identifier
  - `sender_address`: The address from which donation was sent
  - `recipient_address`: The donation receiving address
  - `amount`: Amount donated
  - `record_date`: Date of the donation transaction

## Scheduled Tasks

- **Address Check (`check_addresses`)**  
  Runs every minute to:
  - Verify the number of transactions for each monitored address.
  - Update the balance history and send alerts if a balance change is detected.

- **Monthly Donation Summary (`send_monthly_donation_message`)**  
  Runs on the first day of each month at midnight to compile and send a donation summary to all registered chats.

## 💖 Support the Project

If you appreciate this project, you can support the developer by donating to the following Kaspa address:
`kaspa:qp02azashge2ltj868lmpasj098eul0sphzzvq6am7zx40chug0z6e85xa7ms`

## Contribution

Contributions are welcome!  
If you would like to improve the bot or fix any issues, please consider:
- Opening an issue to report a bug or suggest a new feature.
- Submitting a pull request with your enhancements.

## License

This project is licensed under the [MIT License](LICENSE).
