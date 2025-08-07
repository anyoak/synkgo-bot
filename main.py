import os
import json
import time
import re
import logging
import threading
import asyncio
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from web3 import Web3
from web3.middleware import geth_poa_middleware
import portalocker

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
BSC_RPC = os.getenv('BSC_RPC')
USDT_CONTRACT = os.getenv('USDT_CONTRACT')
PAYMENT_LOG_CHANNEL = int(os.getenv('PAYMENT_LOG_CHANNEL', 2700185457))  # SynkGoPay channel
MOD_LOG_CHANNEL = int(os.getenv('MOD_LOG_CHANNEL', 2368794492))  # Default mod log channel

# Initialize Web3
w3 = Web3(Web3.HTTPProvider(BSC_RPC))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Improved private key validation
def validate_private_key(key: str) -> str:
    """Validate and normalize private key format"""
    if not key:
        raise ValueError("Private key is empty")
    clean_key = key.lower().replace("0x", "").strip()
    if len(clean_key) != 64:
        raise ValueError("Private key must be 64 hexadecimal characters")
    if not re.match(r'^[0-9a-f]{64}$', clean_key):
        raise ValueError("Private key contains invalid characters")
    return clean_key

try:
    PRIVATE_KEY = validate_private_key(PRIVATE_KEY)
    logger.info("Private key format is valid")
    HOT_WALLET_ADDRESS = w3.eth.account.from_key(PRIVATE_KEY).address
    logger.info(f"Hot wallet address: {HOT_WALLET_ADDRESS}")
except Exception as e:
    logger.error(f"Private key error: {e}")
    raise

# Validate and convert USDT contract address
try:
    USDT_CONTRACT = Web3.to_checksum_address(USDT_CONTRACT)
    logger.info(f"Using USDT contract: {USDT_CONTRACT}")
except Exception as e:
    logger.error(f"Invalid USDT contract address: {e}")
    raise

# USDT contract ABI
usdt_abi = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]
contract = w3.eth.contract(address=USDT_CONTRACT, abi=usdt_abi)

# Storage file
DB_FILE = '/data/synkgo_db.json'
logger.info(f"Database location: {DB_FILE}")

# Initialize database
def init_db():
    default_db = {
        "users": {},
        "codes": {},
        "withdrawals": {},
        "gift_codes": {},
        "moderators": {},
        "settings": {
            "reward_per_code": 2,
            "referral_rate": 0.05,
            "min_withdraw": 500,
            "gas_price": 5,
            "gas_limit": 90000,
            "bot_status": "active"
        }
    }
    try:
        if not os.path.exists(DB_FILE):
            logger.info("Creating new database file")
            os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
            with open(DB_FILE, 'w') as f:
                with portalocker.Lock(f.fileno(), timeout=10):
                    json.dump(default_db, f, indent=2)
            logger.info("Database file created successfully")
        else:
            logger.info("Database file already exists")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

# Database helpers
def load_db():
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE) as f:
                with portalocker.Lock(f.fileno(), timeout=10):
                    return json.load(f)
        else:
            init_db()
            return load_db()
    except Exception as e:
        logger.error(f"Failed to load database: {e}")
        init_db()
        return load_db()

def save_db(data):
    try:
        with open(DB_FILE, 'w') as f:
            with portalocker.Lock(f.fileno(), timeout=10):
                json.dump(data, f, indent=2)
        logger.info("Database saved successfully")
    except Exception as e:
        logger.error(f"Failed to save database: {e}")
        raise ValueError(f"Database save failed: {e}")

# Gas optimization
def get_gas_price():
    db = load_db()
    return w3.to_wei(db['settings']['gas_price'], 'gwei')

# USDT transfer function
def send_usdt(to_address, amount_usdt):
    try:
        to_address = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_usdt * 10**18)
        tx = contract.functions.transfer(
            to_address,
            amount_wei
        ).build_transaction({
            'from': HOT_WALLET_ADDRESS,
            'nonce': w3.eth.get_transaction_count(HOT_WALLET_ADDRESS),
            'gasPrice': get_gas_price(),
            'gas': 90000
        })
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"USDT transfer initiated: {tx_hash.hex()} to {to_address} for {amount_usdt} USDT")
        return tx_hash.hex()
    except Exception as e:
        logger.error(f"Transaction failed: {e}, to_address={to_address}, amount={amount_usdt}")
        return None

# Check wallet balance
def get_wallet_balance():
    try:
        bnb_balance = w3.eth.get_balance(HOT_WALLET_ADDRESS)
        usdt_balance = contract.functions.balanceOf(HOT_WALLET_ADDRESS).call()
        return {
            "bnb": w3.from_wei(bnb_balance, 'ether'),
            "usdt": usdt_balance / 10**18
        }
    except Exception as e:
        logger.error(f"Balance check failed: {e}")
        return {"bnb": 0, "usdt": 0}

# Telegram UI Components
def user_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Generate Code", url="https://t.me/+LtWJmPi8I2tkNjQ1")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_start")],
        [InlineKeyboardButton("👥 Referral Program", callback_data="invite_panel")],
        [InlineKeyboardButton("🎁 Gift Code", callback_data="gift_code_panel")],
        [InlineKeyboardButton("📊 My Statistics", callback_data="user_stats")],
        [InlineKeyboardButton("🆘 Support", url="https://t.me/ZenEspt")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Pending Codes", callback_data="admin_pending_codes")],
        [InlineKeyboardButton("👤 User Management", callback_data="admin_user_management")],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("📊 System Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("💼 Wallet Balance", callback_data="admin_wallet_balance")],
        [InlineKeyboardButton("🎁 Gift Codes", callback_data="admin_gift_codes")],
        [InlineKeyboardButton("👮 Moderators", callback_data="admin_moderators")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ])

def moderator_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Pending Codes", callback_data="mod_pending_codes")],
        [InlineKeyboardButton("📋 My Approvals", callback_data="mod_my_approvals")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ])

def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]])

def admin_back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])

def mod_back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Mod Panel", callback_data="mod_panel")]])

# Check if user is banned
def is_banned(user_id: int):
    db = load_db()
    user = db['users'].get(str(user_id), {})
    return user.get('banned', False)

# Check if user is moderator
def is_moderator(user_id: int):
    db = load_db()
    return str(user_id) in db['moderators'] and db['moderators'][str(user_id)].get('status') == 'active'

# Calculate active referrals
def get_active_referrals_count(user_id, db):
    active_count = 0
    today = time.time() - 86400
    referrals = db['users'].get(str(user_id), {}).get('referrals', [])
    for ref_id in referrals:
        ref_user = db['users'].get(str(ref_id))
        if ref_user:
            daily_submissions = sum(
                1 for code_data in db['codes'].values()
                if code_data.get('user_id') == ref_id
                and code_data.get('timestamp', 0) > today
                and code_data.get('status') == 'approved'
            )
            if daily_submissions >= 30:
                active_count += 1
    return active_count

# Process code submission
def process_code_submission(user_id: int, code: str):
    db = load_db()
    settings = db['settings']
    user = db['users'].get(str(user_id), {})
    if user.get('banned', False):
        return "❌ Your account has been banned. Contact @ZenEspt."
    if db['settings']['bot_status'] != 'active':
        return "⛔ Bot is currently under maintenance. Please try again later."
    if not re.match(r'^[A-Za-z0-9]{5,15}$', code):
        return "❌ Invalid code format! Use 5-15 letters/numbers"
    current_time = time.time()
    last_submit = user.get('last_submission', 0)
    cooldown_remaining = 300 - (current_time - last_submit)
    if cooldown_remaining > 0:
        return f"⏳ Please wait {int(cooldown_remaining//60)}m {int(cooldown_remaining%60)}s before submitting another code"
    if user.get('submission_count', 0) >= 30:
        return "❌ You've reached your daily submission limit (30 codes)"
    if code in db['codes']:
        return f"❌ Code '{code}' has already been submitted"
    db['codes'][code] = {
        "status": "pending",
        "user_id": user_id,
        "timestamp": current_time,
        "moderator_id": None,
        "processed_at": None
    }
    user['last_submission'] = current_time
    user['submission_count'] = user.get('submission_count', 0) + 1
    db['users'][str(user_id)] = user
    try:
        save_db(db)
    except ValueError as e:
        logger.error(f"Database save error during code submission: {e}")
        return "❌ Database error, please try again later."
    return (
        f"✅ Code submitted successfully!\n\n"
        f"Code: `{code}`\n"
        f"Status: Pending server approval\n\n"
        f"⏳ _Review may take 5 minutes to 12 hours_"
    )

# Process withdrawal automatically
async def process_withdrawal(context: ContextTypes.DEFAULT_TYPE, user_id: int, points: int, address: str):
    db = load_db()
    withdrawal_id = f"wd_{int(time.time())}_{user_id}"
    
    # Create withdrawal record
    db['withdrawals'][withdrawal_id] = {
        "user_id": user_id,
        "points": points,
        "address": address,
        "status": "processing",
        "timestamp": time.time()
    }
    try:
        save_db(db)
    except ValueError as e:
        logger.error(f"Database save error during withdrawal creation: {e}")
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ *Critical Error*: Failed to save database during withdrawal creation: {e}",
            parse_mode="Markdown"
        )
        return False

    amount_usdt = points * 0.001
    balance = get_wallet_balance()

    # Check wallet balance
    if balance['usdt'] < amount_usdt:
        db['users'][str(user_id)]['balance'] += points
        db['withdrawals'][withdrawal_id]['status'] = 'failed'
        try:
            save_db(db)
        except ValueError as e:
            logger.error(f"Database save error during balance refund: {e}")
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Critical Error*: Failed to save database during balance refund: {e}",
                parse_mode="Markdown"
            )
        await context.bot.send_message(
            user_id,
            f"❌ *Withdrawal Failed*\n\n"
            f"Amount: `{points}` points\n"
            f"Reason: Insufficient USDT in hot wallet\n\n"
            f"Please contact @ZenEspt for assistance.",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ *INSUFFICIENT USDT* ⚠️\n\n"
            f"Withdrawal ID: `{withdrawal_id}`\n"
            f"User: `{user_id}`\n"
            f"Required: `{amount_usdt:.3f}` USDT\n"
            f"Available: `{balance['usdt']:.3f}` USDT\n\n"
            f"Please fund: `{HOT_WALLET_ADDRESS}`",
            parse_mode="Markdown"
        )
        return False

    if balance['bnb'] < 0.001:
        db['users'][str(user_id)]['balance'] += points
        db['withdrawals'][withdrawal_id]['status'] = 'failed'
        try:
            save_db(db)
        except ValueError as e:
            logger.error(f"Database save error during gas refund: {e}")
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Critical Error*: Failed to save database during gas refund: {e}",
                parse_mode="Markdown"
            )
        await context.bot.send_message(
            user_id,
            f"❌ *Withdrawal Failed*\n\n"
            f"Amount: `{points}` points\n"
            f"Reason: Insufficient BNB for gas in hot wallet\n\n"
            f"Please contact @ZenEspt for assistance.",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ *INSUFFICIENT GAS* ⚠️\n\n"
            f"Withdrawal ID: `{withdrawal_id}`\n"
            f"User: `{user_id}`\n"
            f"Required: >0.001 BNB\n"
            f"Available: `{balance['bnb']:.6f}` BNB\n\n"
            f"Please send BNB to: `{HOT_WALLET_ADDRESS}`",
            parse_mode="Markdown"
        )
        return False

    # Attempt transaction with retries
    max_retries = 3
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            tx_hash = send_usdt(address, amount_usdt)
            if tx_hash:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                if receipt['status'] == 1:
                    db['withdrawals'][withdrawal_id]['status'] = 'completed'
                    db['withdrawals'][withdrawal_id]['tx_hash'] = tx_hash
                    db['users'][str(user_id)]['withdrawals'] = db['users'][str(user_id)].get('withdrawals', 0) + 1
                    try:
                        save_db(db)
                    except ValueError as e:
                        logger.error(f"Database save error after successful tx: {e}")
                        await context.bot.send_message(
                            ADMIN_ID,
                            f"⚠️ *Critical Error*: Transaction succeeded but database save failed: {e}",
                            parse_mode="Markdown"
                        )
                        return False
                    
                    user = await context.bot.get_chat(user_id)
                    username = f"@{user.username}" if user.username else user.first_name
                    await context.bot.send_message(
                        user_id,
                        f"✅ *Withdrawal Completed*\n\n"
                        f"Amount: `{amount_usdt:.3f}` USDT\n"
                        f"TX Hash: `{tx_hash}`\n"
                        f"View: https://bscscan.com/tx/{tx_hash}",
                        parse_mode="Markdown"
                    )
                    await context.bot.send_message(
                        PAYMENT_LOG_CHANNEL,
                        f"💸 *Payment Processed*\n\n"
                        f"User: [{username}](tg://user?id={user_id})\n"
                        f"Amount: `{amount_usdt:.3f}` USDT\n"
                        f"TX Hash: `{tx_hash}`\n"
                        f"View: https://bscscan.com/tx/{tx_hash}",
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                    return True
                else:
                    raise ValueError("Transaction failed on blockchain")
            else:
                raise ValueError("Transaction hash not received")
        except Exception as e:
            logger.error(f"Transaction attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                continue
            db['users'][str(user_id)]['balance'] += points
            db['withdrawals'][withdrawal_id]['status'] = 'failed'
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during refund: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Transaction failed and database save failed during refund: {e}",
                    parse_mode="Markdown"
                )
            await context.bot.send_message(
                user_id,
                f"❌ *Withdrawal Failed*\n\n"
                f"Amount: `{points}` points refunded\n"
                f"Reason: Transaction error\n\n"
                f"Please contact @ZenEspt for assistance.",
                parse_mode="Markdown"
            )
            return False
    return False

# Log moderation action to channel
async def log_mod_action(context: ContextTypes.DEFAULT_TYPE, mod_id: int, action: str, code: str, user_id: int, points: int = None):
    try:
        mod_user = await context.bot.get_chat(mod_id)
        mod_name = mod_user.username or mod_user.first_name
        
        target_user = await context.bot.get_chat(user_id)
        target_name = target_user.username or target_user.first_name
        
        message = (
            f"🔰 *Moderation Action*\n\n"
            f"• Moderator: [{mod_name}](tg://user?id={mod_id}) (ID: `{mod_id}`)\n"
            f"• Action: {'Approved' if action == 'approve' else 'Rejected'}\n"
            f"• Code: `{code}`\n"
            f"• User: [{target_name}](tg://user?id={user_id}) (ID: `{user_id}`)"
        )
        
        if action == 'approve' and points:
            message += f"\n• Reward: {points} points"
        
        await context.bot.send_message(
            chat_id=MOD_LOG_CHANNEL,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Failed to log moderation action: {e}")

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("❌ Your account has been banned. Contact @ZenEspt.")
        return
    
    db = load_db()
    if str(user_id) not in db['users']:
        db['users'][str(user_id)] = {
            "balance": 0,
            "codes_submitted": [],
            "submission_count": 0,
            "last_submission": 0,
            "referral_code": f"REF{user_id}",
            "referred_by": None,
            "referrals": [],
            "referral_commission": 0,
            "total_earned": 0,
            "withdrawals": 0,
            "banned": False
        }
    if context.args:
        ref_code = context.args[0]
        if not db['users'][str(user_id)].get('referred_by'):
            for uid, user_data in db['users'].items():
                if user_data.get('referral_code') == ref_code and int(uid) != user_id:
                    if user_id not in user_data.get('referrals', []):
                        db['users'][uid]['referrals'] = user_data.get('referrals', []) + [user_id]
                        db['users'][str(user_id)]['referred_by'] = int(uid)
                        try:
                            save_db(db)
                        except ValueError as e:
                            logger.error(f"Database save error during referral: {e}")
                            await context.bot.send_message(
                                ADMIN_ID,
                                f"⚠️ *Critical Error*: Failed to save database during referral: {e}",
                                parse_mode="Markdown"
                            )
                            return
                        await update.message.reply_text(
                            f"🎉 *Joined via Referral*\n\n"
                            f"You joined using `{ref_code}`!\n"
                            f"You'll help your referrer earn {db['settings']['referral_rate']*100}% commission on your rewards.",
                            parse_mode="Markdown"
                        )
                        await context.bot.send_message(
                            int(uid),
                            f"🎉 *New Referral*\n\n"
                            f"User {user_id} joined using your referral code `{ref_code}`!\n"
                            f"You'll earn {db['settings']['referral_rate']*100}% of their rewards.",
                            parse_mode="Markdown"
                        )
                        break
    try:
        save_db(db)
    except ValueError as e:
        logger.error(f"Database save error during start: {e}")
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ *Critical Error*: Failed to save database during start: {e}",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text(
        "🌟 *Welcome to @SynkGo Rewards Bot!* 🌟\n\n"
        "💰 _Earn points by submitting codes_\n"
        "💸 _Withdraw USDT directly to your wallet_\n"
        "👥 _Invite friends for referral bonuses_\n"
        "🎁 _Claim gift codes for bonus points_\n\n"
        "📝 To submit a code, use /code command\n"
        "Example: `/code ABC12345`\n\n"
        f"💡 Tip: Each approved code earns you {db['settings']['reward_per_code']} points (1 point = 0.001 USDT)",
        parse_mode="Markdown",
        reply_markup=user_panel()
    )

async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("❌ Your account has been banned. Contact @ZenEspt.")
        return
    
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "❌ Please provide a code after the command.\n"
            "Example: /code ABC12345"
        )
        return
    
    code = context.args[0].strip()
    response = process_code_submission(user_id, code)
    
    await update.message.reply_text(response, parse_mode="Markdown")
    
    if "✅" in response:
        user = update.effective_user
        username = f"@{user.username}" if user.username else user.first_name
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        
        admin_message = (
            f"📝 *New Code Submission*\n\n"
            f"• User: [{username}](tg://user?id={user_id}) (ID: `{user_id}`)\n"
            f"• Code: `{code}`\n"
            f"• Time: `{timestamp}`"
        )
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Accept", callback_data=f"approve_code:{code}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_code:{code}")
            ]
        ])
        
        await context.bot.send_message(
            ADMIN_ID,
            admin_message,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ You don't have permission to use this command")
        return
    command = update.message.text.split()[0].lower()
    args = context.args
    db = load_db()
    if command == "/admin":
        await update.message.reply_text("👑 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_panel())
        return
    if command == "/adjust" and len(args) >= 2:
        try:
            target_id = int(args[0])
            amount = int(args[1])
            reason = " ".join(args[2:]) if len(args) > 2 else "No reason provided"
            if str(target_id) not in db['users']:
                db['users'][str(target_id)] = {
                    "balance": 0,
                    "codes_submitted": [],
                    "submission_count": 0,
                    "last_submission": 0,
                    "referral_code": f"REF{target_id}",
                    "referred_by": None,
                    "referrals": [],
                    "referral_commission": 0,
                    "total_earned": 0,
                    "withdrawals": 0,
                    "banned": False
                }
            db['users'][str(target_id)]['balance'] += amount
            db['users'][str(target_id)]['total_earned'] += amount
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during adjust: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during adjust: {e}",
                    parse_mode="Markdown"
                )
                return
            await update.message.reply_text(
                f"✅ Adjusted balance for user {target_id}\n"
                f"Amount: {amount} points\n"
                f"New balance: {db['users'][str(target_id)]['balance']} points\n"
                f"Reason: {reason}"
            )
            await context.bot.send_message(
                target_id,
                f"ℹ️ *Admin Notification*\n\n"
                f"Your balance was adjusted by admin:\n"
                f"Amount: *{amount}* points\n"
                f"New balance: *{db['users'][str(target_id)]['balance']}* points\n"
                f"Reason: {reason}",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid format. Use: /adjust [user_id] [amount] [reason]")
    elif command == "/ban" and len(args) >= 1:
        try:
            target_id = int(args[0])
            reason = " ".join(args[1:]) if len(args) > 1 else "No reason provided"
            if str(target_id) not in db['users']:
                await update.message.reply_text("❌ User not found")
                return
            db['users'][str(target_id)]['banned'] = True
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during ban: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during ban: {e}",
                    parse_mode="Markdown"
                )
                return
            await update.message.reply_text(f"✅ User {target_id} banned\nReason: {reason}")
            await context.bot.send_message(
                target_id,
                f"❌ *Account Suspended*\n\n"
                f"Your account has been banned from using this bot.\n"
                f"Reason: {reason}\n\n"
                f"Contact @ZenEspt if you believe this is a mistake",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid format. Use: /ban [user_id] [reason]")
    elif command == "/unban" and len(args) >= 1:
        try:
            target_id = int(args[0])
            reason = " ".join(args[1:]) if len(args) > 1 else "No reason provided"
            if str(target_id) not in db['users']:
                await update.message.reply_text("❌ User not found")
                return
            db['users'][str(target_id)]['banned'] = False
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during unban: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during unban: {e}",
                    parse_mode="Markdown"
                )
                return
            await update.message.reply_text(f"✅ User {target_id} unbanned\nReason: {reason}")
            await context.bot.send_message(
                target_id,
                f"✅ *Account Restored*\n\n"
                f"Your account has been unbanned.\n"
                f"Reason: {reason}\n\n"
                f"You can now use the bot normally",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid format. Use: /unban [user_id] [reason]")
    elif command == "/settings" and len(args) >= 2:
        setting_name = args[0].lower()
        try:
            new_value = float(args[1])
            if setting_name not in ['reward_per_code', 'referral_rate', 'min_withdraw', 'gas_price']:
                await update.message.reply_text("❌ Invalid setting name")
                return
            db['settings'][setting_name] = new_value
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during settings update: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during settings update: {e}",
                    parse_mode="Markdown"
                )
                return
            await update.message.reply_text(
                f"✅ Setting updated\n"
                f"{setting_name}: {new_value}"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid value format")
    elif command == "/maintenance":
        new_status = "maintenance" if db['settings']['bot_status'] == "active" else "active"
        db['settings']['bot_status'] = new_status
        try:
            save_db(db)
        except ValueError as e:
            logger.error(f"Database save error during maintenance toggle: {e}")
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Critical Error*: Failed to save database during maintenance toggle: {e}",
                parse_mode="Markdown"
            )
            return
        await update.message.reply_text(f"✅ Bot status changed to: {new_status}")
    elif command == "/check" and len(args) >= 1:
        try:
            target_id = int(args[0])
            user = db['users'].get(str(target_id), {})
            points = user.get('balance', 0)
            usdt_value = points * 0.001
            submissions = user.get('submission_count', 0)
            
            await update.message.reply_text(
                f"👤 *User Report*\n\n"
                f"User ID: `{target_id}`\n"
                f"Points: `{points}`\n"
                f"USDT Value: `{usdt_value:.3f}`\n"
                f"Submissions Today: `{submissions}/30`\n"
                f"Total Earned: `{user.get('total_earned',0)}`\n"
                f"Withdrawals: `{user.get('withdrawals',0)}`\n"
                f"Status: `{'BANNED' if user.get('banned') else 'ACTIVE'}`",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID")
    elif command == "/create" and len(args) >= 3:
        try:
            code = args[0].upper()
            points = int(args[1])
            max_claims = int(args[2])
            
            if not re.match(r'^[A-Z0-9]{5,15}$', code):
                await update.message.reply_text("❌ Invalid code format! Use 5-15 uppercase letters/numbers")
                return
                
            if code in db['gift_codes']:
                await update.message.reply_text("❌ Gift code already exists")
                return
                
            db['gift_codes'][code] = {
                "points": points,
                "max_claims": max_claims,
                "claims": 0,
                "created_at": time.time(),
                "created_by": user_id,
                "users_claimed": []
            }
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during gift code creation: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during gift code creation: {e}",
                    parse_mode="Markdown"
                )
                return
            
            await update.message.reply_text(
                f"✅ Gift code created!\n\n"
                f"Code: `{code}`\n"
                f"Points: {points}\n"
                f"Max claims: {max_claims}\n"
                f"Created by: {user_id}",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid format. Use: /create [CODE] [POINTS] [MAX_CLAIMS]")
    elif command == "/refact" and len(args) >= 1:
        try:
            target_id = int(args[0])
            referrer = db['users'].get(str(target_id))
            
            if not referrer:
                await update.message.reply_text("❌ User not found")
                return
            
            settings = db['settings']
            reward_per_code = settings['reward_per_code']
            referral_rate = settings['referral_rate']
            
            today = datetime.now()
            date_range = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(10)]
            date_range.reverse()
            
            response = f"📊 *Referral Activity Report for User {target_id}*\n\n"
            response += "Date       | Active Ref | Total Commission\n"
            response += "----------------------------------------\n"
            
            for date_str in date_range:
                start_time = int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
                end_time = start_time + 86400
                
                active_referrals = 0
                daily_commission = 0.0
                
                for ref_id in referrer.get('referrals', []):
                    ref_user = db['users'].get(str(ref_id))
                    if not ref_user:
                        continue
                    
                    daily_submissions = sum(
                        1 for code_data in db['codes'].values()
                        if code_data.get('user_id') == ref_id
                        and start_time <= code_data.get('timestamp', 0) < end_time
                        and code_data.get('status') == 'approved'
                    )
                    
                    if daily_submissions >= 30:
                        active_referrals += 1
                    
                    daily_commission += daily_submissions * reward_per_code * referral_rate
                
                response += f"{date_str} | {active_referrals:>2}         | {daily_commission:.4f} points\n"
            
            total_referrals = len(referrer.get('referrals', []))
            total_commission = referrer.get('referral_commission', 0)
            
            response += "\n💎 *Summary*\n"
            response += f"Total Referrals: {total_referrals}\n"
            response += f"Lifetime Commission: {total_commission:.4f} points\n"
            response += f"Current Balance: {referrer.get('balance', 0):.4f} points"
            
            await update.message.reply_text(response, parse_mode="Markdown")
            
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID")
    elif command == "/addmod" and len(args) >= 1:
        try:
            mod_id = int(args[0])
            if str(mod_id) in db['moderators']:
                await update.message.reply_text("✅ This user is already a moderator")
                return
                
            db['moderators'][str(mod_id)] = {
                "added_by": user_id,
                "added_at": time.time(),
                "status": "active"
            }
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during mod add: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during mod add: {e}",
                    parse_mode="Markdown"
                )
                return
                
            await update.message.reply_text(f"✅ Added moderator: {mod_id}")
            await context.bot.send_message(
                mod_id,
                "🎉 *Moderator Access Granted*\n\n"
                "You have been added as a moderator!\n"
                "You can now approve/reject user codes.\n\n"
                "Use /mod to access the moderator panel.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID format")
    elif command == "/banmod" and len(args) >= 1:
        try:
            mod_id = int(args[0])
            if str(mod_id) not in db['moderators']:
                await update.message.reply_text("❌ This user is not a moderator")
                return
                
            db['moderators'][str(mod_id)]['status'] = 'inactive'
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during mod ban: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during mod ban: {e}",
                    parse_mode="Markdown"
                )
                return
                
            await update.message.reply_text(f"✅ Moderator {mod_id} deactivated")
            await context.bot.send_message(
                mod_id,
                "⚠️ *Moderator Access Revoked*\n\n"
                "Your moderator privileges have been revoked.",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID format")

async def mod_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_moderator(user_id):
        await update.message.reply_text("❌ You don't have moderator access")
        return
    await update.message.reply_text("👮 *Moderator Panel*", parse_mode="Markdown", reply_markup=moderator_panel())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    db = load_db()
    user = db['users'].get(str(user_id), {})
    if is_banned(user_id):
        await query.edit_message_text("❌ Your account has been banned. Contact @ZenEspt.")
        return
    if data == "main_menu":
        await query.edit_message_text(
            "🌟 *Main Menu* 🌟\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=user_panel()
        )
    elif data == "mod_panel":
        if is_moderator(user_id):
            await query.edit_message_text(
                "👮 *Moderator Panel*",
                parse_mode="Markdown",
                reply_markup=moderator_panel()
            )
    elif data == "withdraw_start":
        min_withdraw = db['settings']['min_withdraw']
        await query.edit_message_text(
            f"💸 *Withdrawal Process*\n\n"
            f"Minimum: {min_withdraw} points = {min_withdraw * 0.001:.3f} USDT\n"
            "Enter withdrawal amount and BEP-20 address in this format:\n\n"
            "`[POINTS] [WALLET_ADDRESS]`\n\n"
            "Example:\n`500 0x742d35Cc6634C05329****44Bc454e4438f44e`\n\n"
            "💡 _1 point = 0.001 USDT_",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
    elif data == "invite_panel":
        ref_code = user.get('referral_code', f"REF{user_id}")
        ref_link = f"https://t.me/{context.bot.username}?start={ref_code}"
        ref_count = len(user.get('referrals', []))
        active_refs = get_active_referrals_count(user_id, db)
        commission = user.get('referral_commission', 0)
        usdt_commission = commission * 0.001
        await query.edit_message_text(
            f"👥 *Referral Program*\n\n"
            f"Your referral code: `{ref_code}`\n"
            f"Your referral link: {ref_link}\n\n"
            f"• Total referrals: {ref_count}\n"
            f"• Active referrals (30+ codes/day): {active_refs}\n"
            f"• Commission earned: {commission:.4f} points ({usdt_commission:.6f} USDT)\n\n"
            f"🔥 _Earn {db['settings']['referral_rate']*100}% of your referrals' earnings!_\n"
            f"✅ _Active referrals submit 30+ approved codes daily_\n"
            f"📬 _You'll be notified when your referrals earn rewards!_",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
    elif data == "user_stats":
        points = user.get('balance', 0)
        usdt_value = points * 0.001
        total_earned = user.get('total_earned', 0)
        submissions = user.get('submission_count', 0)
        await query.edit_message_text(
            f"📊 *Your Statistics*\n\n"
            f"💰 Available Points: {points}\n"
            f"💵 Equivalent USDT: {usdt_value:.3f}\n"
            f"🏆 Total Earned: {total_earned} points\n"
            f"📨 Codes Submitted Today: {submissions}/30\n"
            f"👥 Referrals: {len(user.get('referrals', []))}\n"
            f"🎯 Referral Commission: {user.get('referral_commission', 0):.4f} points",
            reply_markup=back_button()
        )
    elif data == "gift_code_panel":
        await query.edit_message_text(
            "🎁 *Gift Code Center*\n\n"
            "Enter a gift code to claim your points!\n\n"
            "Example: `SYNK500`\n\n"
            "💡 _You can only claim each gift code once_",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
    elif user_id == ADMIN_ID or is_moderator(user_id):
        if data == "admin_panel" and user_id == ADMIN_ID:
            await query.edit_message_text(
                "👑 *Admin Panel*",
                parse_mode="Markdown",
                reply_markup=admin_panel()
            )
        elif data == "mod_pending_codes" and is_moderator(user_id):
            pending_codes = [code for code, data in db['codes'].items() if data['status'] == 'pending']
            if not pending_codes:
                await query.edit_message_text("✅ No pending codes to approve", reply_markup=moderator_panel())
                return
            message = "📝 *Pending Codes*\n\n"
            keyboard = []
            for i, code in enumerate(pending_codes[:10]):
                user_id = db['codes'][code]['user_id']
                message += f"{i+1}. `{code}` (User: {user_id})\n"
                keyboard.append([InlineKeyboardButton(f"✅ Approve {code}", callback_data=f"approve_code:{code}")])
                keyboard.append([InlineKeyboardButton(f"❌ Reject {code}", callback_data=f"reject_code:{code}")])
            keyboard.append([InlineKeyboardButton("🔙 Mod Panel", callback_data="mod_panel")])
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif data == "mod_my_approvals" and is_moderator(user_id):
            mod_codes = [code for code, data in db['codes'].items() 
                        if data.get('moderator_id') == user_id 
                        and data['status'] in ['approved', 'rejected']]
            if not mod_codes:
                await query.edit_message_text("📝 You haven't processed any codes yet", reply_markup=moderator_panel())
                return
            message = "📋 *Your Moderation History*\n\n"
            for code in mod_codes[:20]:
                code_data = db['codes'][code]
                status_icon = "✅" if code_data['status'] == 'approved' else "❌"
                timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(code_data['processed_at']))
                message += f"{status_icon} `{code}` ({timestamp})\n"
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=mod_back_button()
            )
        elif data == "admin_wallet_balance" and user_id == ADMIN_ID:
            balance = get_wallet_balance()
            await query.edit_message_text(
                f"💼 *Hot Wallet Balance*\n\n"
                f"BNB: `{balance['bnb']:.6f}`\n"
                f"USDT: `{balance['usdt']:.2f}`\n\n"
                f"Address: `{HOT_WALLET_ADDRESS}`",
                parse_mode="Markdown",
                reply_markup=admin_panel()
            )
        elif data == "admin_pending_codes" and user_id == ADMIN_ID:
            pending_codes = [code for code, data in db['codes'].items() if data['status'] == 'pending']
            if not pending_codes:
                await query.edit_message_text("✅ No pending codes to approve", reply_markup=admin_panel())
                return
            message = "📝 *Pending Codes*\n\n"
            keyboard = []
            for i, code in enumerate(pending_codes[:10]):
                user_id = db['codes'][code]['user_id']
                message += f"{i+1}. `{code}` (User: {user_id})\n"
                keyboard.append([InlineKeyboardButton(f"✅ Approve {code}", callback_data=f"approve_code:{code}")])
                keyboard.append([InlineKeyboardButton(f"❌ Reject {code}", callback_data=f"reject_code:{code}")])
            keyboard.append([InlineKeyboardButton("✅ Approve All", callback_data="approve_all_codes")])
            keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif data == "approve_all_codes" and user_id == ADMIN_ID:
            pending_codes = [code for code, data in db['codes'].items() if data['status'] == 'pending']
            approved_count = 0
            for code in pending_codes:
                user_id = db['codes'][code]['user_id']
                reward = db['settings']['reward_per_code']
                if str(user_id) in db['users']:
                    db['users'][str(user_id)]['balance'] = db['users'][str(user_id)].get('balance', 0) + reward
                    db['users'][str(user_id)]['total_earned'] = db['users'][str(user_id)].get('total_earned', 0) + reward
                    db['codes'][code]['status'] = 'approved'
                    db['codes'][code]['moderator_id'] = ADMIN_ID
                    db['codes'][code]['processed_at'] = time.time()
                    referrer_id = db['users'].get(str(user_id), {}).get('referred_by')
                    if referrer_id and str(referrer_id) in db['users']:
                        commission = round(reward * db['settings']['referral_rate'], 4)
                        db['users'][str(referrer_id)]['referral_commission'] = db['users'][str(referrer_id)].get('referral_commission', 0) + commission
                        db['users'][str(referrer_id)]['balance'] = db['users'][str(referrer_id)].get('balance', 0) + commission
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 *Referral Commission*\n\n"
                            f"Your referral (User {user_id}) had code `{code}` approved!\n"
                            f"Commission: +{commission:.4f} points ({commission * 0.001:.6f} USDT)\n"
                            f"New balance: {db['users'][str(referrer_id)]['balance']:.4f} points",
                            parse_mode="Markdown"
                        )
                    await context.bot.send_message(
                        user_id,
                        f"🎉 *Code Approved*\n\n"
                        f"Code: `{code}` has been validated!\n"
                        f"Reward: +{reward} points ({reward * 0.001:.3f} USDT)\n"
                        f"New balance: {db['users'][str(user_id)]['balance']} points",
                        parse_mode="Markdown"
                    )
                    approved_count += 1
                    await log_mod_action(context, ADMIN_ID, 'approve', code, user_id, reward)
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during approve all codes: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during approve all codes: {e}",
                    parse_mode="Markdown"
                )
                return
            await query.edit_message_text(f"✅ Approved {approved_count} codes", reply_markup=admin_panel())
        elif data.startswith("approve_code:"):
            code = data.split(":")[1]
            if code in db['codes'] and db['codes'][code]['status'] == 'pending':
                if db['codes'][code].get('moderator_id') is not None:
                    await query.answer("❌ This code is already being processed by another moderator", show_alert=True)
                    return
                    
                db['codes'][code]['moderator_id'] = user_id
                try:
                    save_db(db)
                except ValueError as e:
                    logger.error(f"Database save error during code lock: {e}")
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ *Critical Error*: Failed to save database during code lock: {e}",
                        parse_mode="Markdown"
                    )
                    return
                    
                user_id = db['codes'][code]['user_id']
                reward = db['settings']['reward_per_code']
                if str(user_id) in db['users']:
                    db['users'][str(user_id)]['balance'] = db['users'][str(user_id)].get('balance', 0) + reward
                    db['users'][str(user_id)]['total_earned'] = db['users'][str(user_id)].get('total_earned', 0) + reward
                    db['codes'][code]['status'] = 'approved'
                    db['codes'][code]['processed_at'] = time.time()
                    referrer_id = db['users'].get(str(user_id), {}).get('referred_by')
                    if referrer_id and str(referrer_id) in db['users']:
                        commission = round(reward * db['settings']['referral_rate'], 4)
                        db['users'][str(referrer_id)]['referral_commission'] = db['users'][str(referrer_id)].get('referral_commission', 0) + commission
                        db['users'][str(referrer_id)]['balance'] = db['users'][str(referrer_id)].get('balance', 0) + commission
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 *Referral Commission*\n\n"
                            f"Your referral (User {user_id}) had code `{code}` approved!\n"
                            f"Commission: +{commission:.4f} points ({commission * 0.001:.6f} USDT)\n"
                            f"New balance: {db['users'][str(referrer_id)]['balance']:.4f} points",
                            parse_mode="Markdown"
                        )
                    try:
                        save_db(db)
                    except ValueError as e:
                        logger.error(f"Database save error during code approval: {e}")
                        await context.bot.send_message(
                            ADMIN_ID,
                            f"⚠️ *Critical Error*: Failed to save database during code approval: {e}",
                            parse_mode="Markdown"
                        )
                        return
                    await log_mod_action(context, user_id, 'approve', code, user_id, reward)
                    
                    if user_id == ADMIN_ID:
                        panel = admin_panel()
                    else:
                        panel = moderator_panel()
                        
                    await query.edit_message_text(
                        f"✅ Code `{code}` approved! User received {reward} points.",
                        reply_markup=panel
                    )
                    await context.bot.send_message(
                        db['codes'][code]['user_id'],
                        f"🎉 *Code Approved*\n\n"
                        f"Code: `{code}` has been validated!\n"
                        f"Reward: +{reward} points ({reward * 0.001:.3f} USDT)\n"
                        f"New balance: {db['users'][str(user_id)]['balance']} points",
                        parse_mode="Markdown"
                    )
                else:
                    await query.edit_message_text("❌ User not found", reply_markup=moderator_panel() if is_moderator(user_id) else admin_panel())
            else:
                await query.edit_message_text("❌ Code not found or already approved", reply_markup=moderator_panel() if is_moderator(user_id) else admin_panel())
        elif data.startswith("reject_code:"):
            code = data.split(":")[1]
            if code in db['codes'] and db['codes'][code]['status'] == 'pending':
                if db['codes'][code].get('moderator_id') is not None:
                    await query.answer("❌ This code is already being processed by another moderator", show_alert=True)
                    return
                    
                db['codes'][code]['moderator_id'] = user_id
                try:
                    save_db(db)
                except ValueError as e:
                    logger.error(f"Database save error during code lock: {e}")
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ *Critical Error*: Failed to save database during code lock: {e}",
                        parse_mode="Markdown"
                    )
                    return
                    
                user_id = db['codes'][code]['user_id']
                db['codes'][code]['status'] = 'rejected'
                db['codes'][code]['processed_at'] = time.time()
                try:
                    save_db(db)
                except ValueError as e:
                    logger.error(f"Database save error during code rejection: {e}")
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ *Critical Error*: Failed to save database during code rejection: {e}",
                        parse_mode="Markdown"
                    )
                    return
                await log_mod_action(context, user_id, 'reject', code, user_id)
                
                if user_id == ADMIN_ID:
                    panel = admin_panel()
                else:
                    panel = moderator_panel()
                    
                await query.edit_message_text(
                    f"❌ Code `{code}` rejected", 
                    reply_markup=panel
                )
                await context.bot.send_message(
                    user_id,
                    f"❌ *Code Rejected*\n\n"
                    f"Code: `{code}` is invalid!\n"
                    f"ℹ️ Please regenerate a new code from the correct source and try again.",
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text("❌ Code not found or already processed", reply_markup=moderator_panel() if is_moderator(user_id) else admin_panel())
        elif data == "admin_user_management" and user_id == ADMIN_ID:
            await query.edit_message_text(
                "👤 *User Management*\n\n"
                "Select an action:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Points", callback_data="admin_add_points")],
                    [InlineKeyboardButton("➖ Remove Points", callback_data="admin_remove_points")],
                    [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_user")],
                    [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_user")],
                    [InlineKeyboardButton("📊 View User Stats", callback_data="admin_view_user")],
                    [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]
                ])
            )
        elif data == "admin_settings" and user_id == ADMIN_ID:
            settings = db['settings']
            await query.edit_message_text(
                f"⚙️ *Bot Settings*\n\n"
                f"• Reward per code: `{settings['reward_per_code']}`\n"
                f"• Referral rate: `{settings['referral_rate']*100}%`\n"
                f"• Min withdrawal: `{settings['min_withdraw']}` points\n"
                f"• Gas price: `{settings['gas_price']}` Gwei\n"
                f"• Bot status: `{settings['bot_status']}`\n\n"
                "Use commands to change settings:\n"
                "`/settings [name] [value]`\n"
                "`/maintenance` to toggle status",
                parse_mode="Markdown",
                reply_markup=admin_panel()
            )
        elif data == "admin_stats" and user_id == ADMIN_ID:
            total_users = len(db['users'])
            active_today = sum(1 for u in db['users'].values() if u.get('submission_count', 0) > 0)
            total_points = sum(u['balance'] for u in db['users'].values())
            pending_codes = sum(1 for c in db['codes'].values() if c['status'] == 'pending')
            pending_wds = sum(1 for w in db['withdrawals'].values() if w['status'] == 'pending')
            gift_codes = len(db['gift_codes'])
            active_mods = sum(1 for m in db['moderators'].values() if m.get('status') == 'active')
            await query.edit_message_text(
                f"📊 *System Statistics*\n\n"
                f"• Total users: `{total_users}`\n"
                f"• Active today: `{active_today}`\n"
                f"• Total points in circulation: `{total_points}`\n"
                f"• Pending codes: `{pending_codes}`\n"
                f"• Pending withdrawals: `{pending_wds}`\n"
                f"• Active gift codes: `{gift_codes}`\n"
                f"• Active moderators: `{active_mods}`\n"
                f"• Last updated: {time.ctime()}",
                parse_mode="Markdown",
                reply_markup=admin_panel()
            )
        elif data == "admin_gift_codes" and user_id == ADMIN_ID:
            if not db['gift_codes']:
                await query.edit_message_text("❌ No active gift codes", reply_markup=admin_back_button())
                return
                
            message = "🎁 *Active Gift Codes*\n\n"
            for code, details in db['gift_codes'].items():
                created_time = time.strftime("%Y-%m-%d", time.localtime(details['created_at']))
                message += (
                    f"• `{code}`: {details['points']} points\n"
                    f"  Claims: {details['claims']}/{details['max_claims']}\n"
                    f"  Created: {created_time} by {details['created_by']}\n\n"
                )
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=admin_back_button()
            )
        elif data == "admin_moderators" and user_id == ADMIN_ID:
            if not db['moderators']:
                await query.edit_message_text("❌ No moderators added yet", reply_markup=admin_back_button())
                return
                
            message = "👮 *Active Moderators*\n\n"
            for mod_id, details in db['moderators'].items():
                if details.get('status') == 'active':
                    added_time = time.strftime("%Y-%m-%d", time.localtime(details['added_at']))
                    message += (
                        f"• User ID: `{mod_id}`\n"
                        f"  Added by: `{details['added_by']}`\n"
                        f"  Added on: {added_time}\n\n"
                    )
            await query.edit_message_text(
                message,
                parse_mode="Markdown",
                reply_markup=admin_back_button()
            )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if is_banned(user_id):
        await update.message.reply_text("❌ Your account has been banned. Contact @ZenEspt.")
        return
    text = update.message.text.strip()
    
    if text and len(text.split()) >= 2:
        parts = text.split()
        try:
            points = int(parts[0])
            address = ' '.join(parts[1:])
            db = load_db()
            
            if str(user_id) not in db['users']:
                db['users'][str(user_id)] = {
                    "balance": 0,
                    "codes_submitted": [],
                    "submission_count": 0,
                    "last_submission": 0,
                    "referral_code": f"REF{user_id}",
                    "referred_by": None,
                    "referrals": [],
                    "referral_commission": 0,
                    "total_earned": 0,
                    "withdrawals": 0,
                    "banned": False
                }
            
            user = db['users'][str(user_id)]
            min_withdraw = db['settings']['min_withdraw']
            
            if points < min_withdraw:
                await update.message.reply_text(
                    f"❌ Minimum withdrawal is {min_withdraw} points",
                    reply_markup=back_button()
                )
                return
            
            pending_withdrawals = [
                wd for wd in db['withdrawals'].values() 
                if wd['user_id'] == user_id and wd['status'] == 'pending'
            ]
            
            if pending_withdrawals:
                await update.message.reply_text(
                    "⏳ You already have a pending withdrawal!",
                    reply_markup=back_button()
                )
                return
            
            if points > user['balance']:
                await update.message.reply_text(
                    "❌ Insufficient balance",
                    reply_markup=back_button()
                )
                return
            
            if not Web3.is_address(address):
                await update.message.reply_text(
                    "❌ Invalid wallet address format",
                    reply_markup=back_button()
                )
                return
            
            user['balance'] -= points
            try:
                save_db(db)
            except ValueError as e:
                logger.error(f"Database save error during withdrawal initiation: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to save database during withdrawal initiation: {e}",
                    parse_mode="Markdown"
                )
                return
            
            await update.message.reply_text(
                f"🔄 *Processing Withdrawal*\n\n"
                f"Points: `{points}`\n"
                f"Amount: `{points * 0.001:.3f}` USDT\n"
                f"Address: `{address}`\n"
                f"_Please wait for transaction confirmation..._",
                parse_mode="Markdown",
                reply_markup=back_button()
            )
            
            success = await process_withdrawal(context, user_id, points, address)
            if not success:
                await update.message.reply_text(
                    f"❌ *Withdrawal Failed*\n\n"
                    f"Points: `{points}` refunded\n"
                    f"Reason: Transaction error or insufficient funds\n\n"
                    f"Please contact @ZenEspt for assistance.",
                    parse_mode="Markdown",
                    reply_markup=back_button()
                )
                
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Use: [POINTS] [WALLET_ADDRESS]",
                reply_markup=back_button()
            )
    
    elif text and len(text.split()) == 1:
        code = text.upper()
        db = load_db()
        
        if str(user_id) not in db['users']:
            db['users'][str(user_id)] = {
                "balance": 0,
                "codes_submitted": [],
                "submission_count": 0,
                "last_submission": 0,
                "referral_code": f"REF{user_id}",
                "referred_by": None,
                "referrals": [],
                "referral_commission": 0,
                "total_earned": 0,
                "withdrawals": 0,
                "banned": False
            }
        
        if code not in db['gift_codes']:
            await update.message.reply_text(
                "❌ Invalid gift code",
                reply_markup=back_button()
            )
            return
            
        gift = db['gift_codes'][code]
        
        if gift['claims'] >= gift['max_claims']:
            await update.message.reply_text(
                "❌ This gift code has reached its claim limit",
                reply_markup=back_button()
            )
            return
            
        if user_id in gift['users_claimed']:
            await update.message.reply_text(
                "❌ You've already claimed this gift code",
                reply_markup=back_button()
            )
            return
            
        user = db['users'][str(user_id)]
        points = gift['points']
        
        user['balance'] += points
        user['total_earned'] += points
        gift['claims'] += 1
        gift['users_claimed'].append(user_id)
        
        try:
            save_db(db)
        except ValueError as e:
            logger.error(f"Database save error during gift code claim: {e}")
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Critical Error*: Failed to save database during gift code claim: {e}",
                parse_mode="Markdown"
            )
            return
        
        await update.message.reply_text(
            f"🎉 *Gift Code Claimed!*\n\n"
            f"You received *{points}* points!\n"
            f"New balance: *{user['balance']}* points",
            parse_mode="Markdown",
            reply_markup=back_button()
        )
        
        await context.bot.send_message(
            ADMIN_ID,
            f"🎁 Gift Code Claimed\n\n"
            f"User: `{user_id}`\n"
            f"Code: `{code}`\n"
            f"Points: {points}\n"
            f"Claims: {gift['claims']}/{gift['max_claims']}",
            parse_mode="Markdown"
        )

async def check_stuck_withdrawals(context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    current_time = time.time()
    stuck_threshold = 3600
    
    for wd_id, withdrawal in db['withdrawals'].items():
        if withdrawal['status'] == 'processing' and (current_time - withdrawal['timestamp']) > stuck_threshold:
            user_id = withdrawal['user_id']
            points = withdrawal['points']
            withdrawal['status'] = 'failed'
            db['users'][str(user_id)]['balance'] += points
            try:
                save_db(db)
                await context.bot.send_message(
                    user_id,
                    f"❌ *Withdrawal Failed*\n\n"
                    f"ID: `{wd_id}`\n"
                    f"Amount: {points} points refunded\n"
                    f"Reason: Processing timeout\n\n"
                    f"Please contact @ZenEspt for assistance.",
                    parse_mode="Markdown"
                )
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Stuck Withdrawal Detected*\n\n"
                    f"ID: `{wd_id}`\n"
                    f"User: `{user_id}`\n"
                    f"Points refunded: {points}",
                    parse_mode="Markdown"
                )
            except ValueError as e:
                logger.error(f"Database save error during stuck withdrawal handling: {e}")
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ *Critical Error*: Failed to handle stuck withdrawal {wd_id}: {e}",
                    parse_mode="Markdown"
                )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.error(f"Error: {context.error}", exc_info=context.error)
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ An error occurred. Please try again later."
            )
        else:
            logger.error("Error without a valid message update")
    except Exception as e:
        logger.error(f"Error in error handler: {e}")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')

def run_health_server():
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('', port), HealthCheckHandler)
    logger.info(f"Health check server running on port {port}")
    server.serve_forever()

def main():
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return
    
    if 'PORT' in os.environ:
        health_thread = threading.Thread(target=run_health_server, daemon=True)
        health_thread.start()
        logger.info("Started health check server")
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("code", code_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("adjust", admin_command))
    application.add_handler(CommandHandler("ban", admin_command))
    application.add_handler(CommandHandler("unban", admin_command))
    application.add_handler(CommandHandler("settings", admin_command))
    application.add_handler(CommandHandler("maintenance", admin_command))
    application.add_handler(CommandHandler("check", admin_command))
    application.add_handler(CommandHandler("create", admin_command))
    application.add_handler(CommandHandler("refact", admin_command))
    application.add_handler(CommandHandler("addmod", admin_command))
    application.add_handler(CommandHandler("banmod", admin_command))
    application.add_handler(CommandHandler("mod", mod_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    application.job_queue.run_repeating(check_stuck_withdrawals, interval=600, first=60)
    
    logger.info("Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
