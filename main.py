import os
import json
import time
import re
import logging
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from web3 import Web3
from web3.middleware import geth_poa_middleware

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

# Initialize Web3
w3 = Web3(Web3.HTTPProvider(BSC_RPC))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# Improved private key validation
def validate_private_key(key: str) -> str:
    """Validate and normalize private key format"""
    if not key:
        raise ValueError("Private key is empty")
    
    # Remove 0x prefix if present
    clean_key = key.lower().replace("0x", "").strip()
    
    # Validate length (64 hex characters = 32 bytes)
    if len(clean_key) != 64:
        raise ValueError("Private key must be 64 hexadecimal characters")
    
    # Validate hex format
    if not re.match(r'^[0-9a-f]{64}$', clean_key):
        raise ValueError("Private key contains invalid characters")
    
    return clean_key

try:
    # Validate and normalize private key
    PRIVATE_KEY = validate_private_key(PRIVATE_KEY)
    logger.info("Private key format is valid")
    
    # Derive hot wallet address from private key
    HOT_WALLET_ADDRESS = w3.eth.account.from_key(PRIVATE_KEY).address
    logger.info(f"Hot wallet address: {HOT_WALLET_ADDRESS}")
except Exception as e:
    logger.error(f"Private key error: {e}")
    raise

# Validate and convert USDT contract address to checksum format
try:
    USDT_CONTRACT = Web3.to_checksum_address(USDT_CONTRACT)
    logger.info(f"Using USDT contract: {USDT_CONTRACT}")
except Exception as e:
    logger.error(f"Invalid USDT contract address: {e}")
    raise

# Load USDT contract ABI
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

# Storage file - use absolute path in home directory
DB_FILE = os.path.join(os.path.expanduser('~'), 'db.json')
logger.info(f"Database location: {DB_FILE}")

# Initialize database
def init_db():
    default_db = {
        "users": {},
        "codes": {},
        "withdrawals": {},
        "settings": {
            "reward_per_code": 2,
            "referral_rate": 0.05,
            "min_withdraw": 500,
            "gas_price": 5,  # in Gwei
            "gas_limit": 90000
        }
    }
    try:
        if not os.path.exists(DB_FILE):
            logger.info("Creating new database file")
            with open(DB_FILE, 'w') as f:
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
        with open(DB_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        # Create new database if not found
        init_db()
        return load_db()
    except Exception as e:
        logger.error(f"Failed to load database: {e}")
        # Return empty database structure
        return {
            "users": {},
            "codes": {},
            "withdrawals": {},
            "settings": {
                "reward_per_code": 2,
                "referral_rate": 0.05,
                "min_withdraw": 500,
                "gas_price": 5,
                "gas_limit": 90000
            }
        }

def save_db(data):
    try:
        with open(DB_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save database: {e}")

# Gas optimization for transactions
def get_gas_price():
    db = load_db()
    return w3.to_wei(db['settings']['gas_price'], 'gwei')

# USDT transfer function
def send_usdt(to_address, amount_usdt):
    try:
        # Convert to checksum address
        to_address = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_usdt * 10**18)
        
        # Build transaction
        tx = contract.functions.transfer(
            to_address, 
            amount_wei
        ).build_transaction({
            'from': HOT_WALLET_ADDRESS,
            'nonce': w3.eth.get_transaction_count(HOT_WALLET_ADDRESS),
            'gasPrice': get_gas_price(),
            'gas': 90000
        })
        
        # Sign and send
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        return tx_hash.hex()
    except Exception as e:
        logger.error(f"Transaction failed: {e}")
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
        [InlineKeyboardButton("💾 Submit Code", switch_inline_query_current_chat="submit_code ")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw_start")],
        [InlineKeyboardButton("👥 Invite", callback_data="invite_panel")],
        [InlineKeyboardButton("🆘 Support Group", url="https://t.me/SynkGoChat")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Approve Codes", callback_data="admin_approve_codes")],
        [InlineKeyboardButton("✅ Approve Withdrawals", callback_data="admin_approve_withdrawals")],
        [InlineKeyboardButton("👤 Adjust Balances", callback_data="admin_adjust_balance")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("💼 Wallet Balance", callback_data="admin_wallet_balance")]
    ])

def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]])

# Membership verification
async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    channels = ["@SynkGo", "@SynkGoPay"]
    
    for channel in channels:
        try:
            member = await context.bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logger.error(f"Membership check error: {e}")
            return False
    return True

# Calculate active referrals (submitted 30+ codes today)
def get_active_referrals_count(user_id, db):
    active_count = 0
    today = time.time() - 86400  # 24 hours ago
    
    # Get the user's referrals
    referrals = db['users'].get(str(user_id), {}).get('referrals', [])
    
    for ref_id in referrals:
        ref_user = db['users'].get(str(ref_id))
        if ref_user:
            # Count submissions in the last 24 hours
            daily_submissions = sum(
                1 for code_data in db['codes'].values() 
                if code_data.get('user_id') == ref_id and 
                   code_data.get('timestamp', 0) > today and
                   code_data.get('status') == 'approved'
            )
            if daily_submissions >= 30:
                active_count += 1
                
    return active_count

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_membership(update, context):
        await update.message.reply_text(
            "⚠️ Please join our official channels to use this bot:\n"
            "- @SynkGo\n"
            "- @SynkGoPay\n\n"
            "Join them and try again!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join @SynkGo", url="https://t.me/SynkGo")],
                [InlineKeyboardButton("Join @SynkGoPay", url="https://t.me/SynkGoPay")]
            ])
        )
        return
    
    user_id = update.effective_user.id
    db = load_db()
    
    # Initialize user if new
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
            "total_earned": 0
        }
        save_db(db)
    
    # Check for referral code
    if context.args:
        ref_code = context.args[0]
        if ref_code != db['users'][str(user_id)].get('referral_code'):
            # Find referrer
            for uid, user in db['users'].items():
                if user.get('referral_code') == ref_code:
                    # Add to referrer's referrals
                    if user_id not in user['referrals']:
                        db['users'][uid]['referrals'].append(user_id)
                    # Set referred_by for current user
                    db['users'][str(user_id)]['referred_by'] = ref_code
                    save_db(db)
                    break
    
    await update.message.reply_text(
        "🌟 Welcome to SynkGo Rewards Bot! 🌟\n\n"
        "Earn points by submitting codes and withdraw USDT!\n"
        "Invite friends for referral bonuses!",
        reply_markup=user_panel()
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    results = []
    
    # Code submission handler
    if query.lower().startswith("submit_code"):
        code = query[11:].strip()
        user_id = update.inline_query.from_user.id
        
        if not code:
            results.append(InlineQueryResultArticle(
                id="invalid",
                title="Code Submission",
                input_message_content=InputTextMessageContent(
                    "⚠️ Please enter a code after 'submit_code'"
                )
            ))
        else:
            db = load_db()
            settings = db['settings']
            user = db['users'].get(str(user_id), {})
            
            # Check cooldown
            current_time = time.time()
            last_submit = user.get('last_submission', 0)
            cooldown_remaining = 300 - (current_time - last_submit)
            
            if cooldown_remaining > 0:
                results.append(InlineQueryResultArticle(
                    id="cooldown",
                    title="Cooldown Active",
                    input_message_content=InputTextMessageContent(
                        f"⏳ Please wait {int(cooldown_remaining//60)}m {int(cooldown_remaining%60)}s "
                        "before submitting another code"
                    )
                ))
            # Check daily limit
            elif user.get('submission_count', 0) >= 30:
                results.append(InlineQueryResultArticle(
                    id="limit",
                    title="Daily Limit Reached",
                    input_message_content=InputTextMessageContent(
                        "❌ You've reached your daily submission limit (30 codes)"
                    )
                ))
            # Check duplicate
            elif code in db['codes']:
                results.append(InlineQueryResultArticle(
                    id="duplicate",
                    title="Duplicate Code",
                    input_message_content=InputTextMessageContent(
                        f"❌ Code '{code}' has already been submitted"
                    )
                ))
            else:
                results.append(InlineQueryResultArticle(
                    id="submit",
                    title=f"Submit Code: {code}",
                    input_message_content=InputTextMessageContent(
                        f"✅ Code '{code}' submitted for approval!"
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 User Panel", callback_data="main_menu")]
                    ])
                ))
                # Save to database
                db['codes'][code] = {
                    "status": "pending",
                    "user_id": user_id,
                    "timestamp": current_time
                }
                user['last_submission'] = current_time
                user['submission_count'] = user.get('submission_count', 0) + 1
                db['users'][str(user_id)] = user
                save_db(db)
    
    await update.inline_query.answer(results, cache_time=0)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    db = load_db()
    user = db['users'].get(str(user_id), {})
    
    # Main menu
    if data == "main_menu":
        await query.edit_message_text(
            "🌟 Main Menu 🌟\nChoose an option:",
            reply_markup=user_panel()
        )
    
    # Withdraw flow
    elif data == "withdraw_start":
        min_withdraw = db['settings']['min_withdraw']
        await query.edit_message_text(
            f"💸 Withdrawal Process\n\n"
            f"Minimum: {min_withdraw} points = {min_withdraw * 0.001:.3f} USDT\n"
            "Enter withdrawal amount and BEP-20 address in this format:\n\n"
            "<code>[POINTS] [WALLET_ADDRESS]</code>\n\n"
            "Example: <code>500 0x742d35Cc6634C0532925a3b844Bc454e4438f44e</code>",
            parse_mode="HTML",
            reply_markup=back_button()
        )
    
    # Invite panel
    elif data == "invite_panel":
        ref_code = user.get('referral_code', f"REF{user_id}")
        ref_link = f"https://t.me/{context.bot.username}?start={ref_code}"
        ref_count = len(user.get('referrals', []))
        commission = user.get('referral_commission', 0)
        active_refs = get_active_referrals_count(user_id, db)
        
        await query.edit_message_text(
            f"👥 Referral Program\n\n"
            f"Your referral code: <code>{ref_code}</code>\n"
            f"Your referral link: {ref_link}\n\n"
            f"• Total referrals: {ref_count}\n"
            f"• Active referrals (30+ codes today): {active_refs}\n"
            f"• Commission earned: {commission} points\n\n"
            "Earn 5% of your referrals' point earnings!",
            parse_mode="HTML",
            reply_markup=back_button()
        )
    
    # Admin features
    elif user_id == ADMIN_ID:
        if data == "admin_panel":
            await query.edit_message_text(
                "👑 Admin Panel",
                reply_markup=admin_panel()
            )
        
        elif data == "admin_wallet_balance":
            balance = get_wallet_balance()
            await query.edit_message_text(
                f"💼 Hot Wallet Balance\n\n"
                f"BNB: {balance['bnb']:.6f}\n"
                f"USDT: {balance['usdt']:.2f}\n\n"
                f"Address: <code>{HOT_WALLET_ADDRESS}</code>",
                parse_mode="HTML",
                reply_markup=admin_panel()
            )
    
    # Handle back button
    elif data == "back":
        await query.edit_message_text(
            "🌟 Main Menu 🌟",
            reply_markup=user_panel()
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    db = load_db()
    user = db['users'].get(str(user_id), {})
    
    # Withdrawal processing
    if text and len(text.split()) >= 2:
        parts = text.split()
        try:
            points = int(parts[0])
            address = parts[1]
            
            # Validate points
            min_withdraw = db['settings']['min_withdraw']
            if points < min_withdraw:
                await update.message.reply_text(
                    f"❌ Minimum withdrawal is {min_withdraw} points",
                    reply_markup=back_button()
                )
                return
            
            # Validate address
            if not Web3.is_address(address):
                await update.message.reply_text(
                    "❌ Invalid wallet address format",
                    reply_markup=back_button()
                )
                return
            
            # Check balance
            if points > user.get('balance', 0):
                await update.message.reply_text(
                    "❌ Insufficient balance",
                    reply_markup=back_button()
                )
                return
            
            # Create withdrawal request
            withdrawal_id = f"wd_{int(time.time())}"
            db['withdrawals'][withdrawal_id] = {
                "user_id": user_id,
                "points": points,
                "address": address,
                "status": "pending",
                "timestamp": time.time()
            }
            save_db(db)
            
            # Confirmation message
            await update.message.reply_text(
                f"✅ Withdrawal request created!\n\n"
                f"Points: {points}\n"
                f"Amount: {points * 0.001:.3f} USDT\n"
                f"Address: <code>{address}</code>\n\n"
                "Waiting for admin approval...",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 User Panel", callback_data="main_menu")]
                ])
            )
            
            # Notify admin
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ New Withdrawal Request\n\n"
                f"User: {user_id}\n"
                f"Points: {points}\n"
                f"Amount: {points * 0.001:.3f} USDT\n"
                f"Address: {address}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_wd:{withdrawal_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_wd:{withdrawal_id}")
                    ]
                ])
            )
            
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Use: [POINTS] [WALLET_ADDRESS]",
                reply_markup=back_button()
            )
    
    # Referral link handling
    elif text.startswith("/start "):
        ref_code = text.split()[1]
        db = load_db()
        
        # Check if referral code exists
        if any(u['referral_code'] == ref_code for u in db['users'].values()):
            user['referred_by'] = ref_code
            db['users'][str(user_id)] = user
            save_db(db)
            await update.message.reply_text(
                "🎉 Referral link activated! You'll earn 5% of your friend's rewards!",
                reply_markup=user_panel()
            )

async def process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE, withdrawal_id: str):
    db = load_db()
    withdrawal = db['withdrawals'].get(withdrawal_id)
    
    if not withdrawal:
        await update.callback_query.answer("Withdrawal not found")
        return
    
    user_id = withdrawal['user_id']
    points = withdrawal['points']
    address = withdrawal['address']
    
    # Deduct points
    if str(user_id) in db['users']:
        db['users'][str(user_id)]['balance'] -= points
    
    # Process transaction
    amount_usdt = points * 0.001
    tx_hash = send_usdt(address, amount_usdt)
    
    if tx_hash:
        withdrawal['status'] = "completed"
        withdrawal['tx_hash'] = tx_hash
        save_db(db)
        
        # Notify user
        await context.bot.send_message(
            user_id,
            f"✅ Withdrawal completed!\n\n"
            f"Amount: {amount_usdt:.3f} USDT\n"
            f"TX Hash: {tx_hash}\n"
            f"View on BscScan: https://bscscan.com/tx/{tx_hash}"
        )
        
        # Notify channel
        await context.bot.send_message(
            "@SynkGoPay",
            f"💸 New Withdrawal\n\n"
            f"User: {user_id}\n"
            f"Amount: {amount_usdt:.3f} USDT\n"
            f"TX Hash: {tx_hash}\n"
            f"Address: {address}"
        )
        
        await update.callback_query.edit_message_text(
            f"✅ Withdrawal processed\nTX Hash: {tx_hash}"
        )
    else:
        await update.callback_query.edit_message_text(
            "❌ Transaction failed. Check logs."
        )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("approve_wd:"):
        withdrawal_id = data.split(":")[1]
        await process_withdrawal(update, context, withdrawal_id)
    elif data.startswith("reject_wd:"):
        withdrawal_id = data.split(":")[1]
        db = load_db()
        if withdrawal_id in db['withdrawals']:
            db['withdrawals'][withdrawal_id]['status'] = 'rejected'
            save_db(db)
            await query.edit_message_text(f"Withdrawal {withdrawal_id} rejected.")
            # Notify user
            user_id = db['withdrawals'][withdrawal_id]['user_id']
            await context.bot.send_message(
                user_id,
                f"❌ Your withdrawal request {withdrawal_id} has been rejected."
            )
    elif data == "admin_approve_codes":
        db = load_db()
        pending_codes = [code for code, data in db['codes'].items() if data['status'] == 'pending']
        
        if not pending_codes:
            await query.edit_message_text("No pending codes to approve.")
            return
        
        message = "📝 Pending Codes:\n\n"
        keyboard = []
        
        for i, code in enumerate(pending_codes[:10]):  # Show first 10
            user_id = db['codes'][code]['user_id']
            message += f"{i+1}. {code} (User: {user_id})\n"
            keyboard.append([InlineKeyboardButton(f"Approve {code}", callback_data=f"approve_code:{code}")])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        
        await query.edit_message_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard)
    elif data.startswith("approve_code:"):
        code = data.split(":")[1]
        db = load_db()
        
        if code in db['codes'] and db['codes'][code]['status'] == 'pending':
            user_id = db['codes'][code]['user_id']
            reward = db['settings']['reward_per_code']
            
            # Add points to user
            if str(user_id) in db['users']:
                db['users'][str(user_id)]['balance'] = db['users'][str(user_id)].get('balance', 0) + reward
                db['users'][str(user_id)]['total_earned'] = db['users'][str(user_id)].get('total_earned', 0) + reward
            
            # Update code status
            db['codes'][code]['status'] = 'approved'
            
            # Handle referral commission
            referrer_id = None
            user_data = db['users'].get(str(user_id))
            if user_data and user_data.get('referred_by'):
                # Find referrer by referral code
                for uid, u in db['users'].items():
                    if u.get('referral_code') == user_data['referred_by']:
                        referrer_id = uid
                        break
            
            if referrer_id:
                commission = int(reward * db['settings']['referral_rate'])
                db['users'][str(referrer_id)]['referral_commission'] += commission
                db['users'][str(referrer_id)]['balance'] += commission
                # Notify referrer
                await context.bot.send_message(
                    referrer_id,
                    f"🎉 You earned {commission} points referral commission!\n"
                    f"From user {user_id} submitting code {code}"
                )
            
            save_db(db)
            await query.edit_message_text(f"✅ Code {code} approved! User received {reward} points.")
        else:
            await query.edit_message_text("❌ Code not found or already approved.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")
    if update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An error occurred. Please try again later."
        )

# Wallet balance monitor
async def wallet_monitor(context: ContextTypes.DEFAULT_TYPE):
    balance = get_wallet_balance()
    if balance['usdt'] < 5 or balance['bnb'] < 0.01:
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ LOW WALLET BALANCE ⚠️\n\n"
            f"BNB: {balance['bnb']:.6f}\n"
            f"USDT: {balance['usdt']:.2f}\n\n"
            "Add funds immediately!"
        )

def main():
    # Initialize database
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    
    # Create application with job queue support
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(InlineQueryHandler(inline_query))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(admin_|approve_|reject_)"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    # Wallet monitor job
    if application.job_queue:
        application.job_queue.run_repeating(wallet_monitor, interval=3600, first=10)
        logger.info("Wallet monitor job scheduled")
    else:
        logger.error("Job queue not available. Wallet monitoring disabled.")
    
    # Start bot
    application.run_polling()

if __name__ == "__main__":
    main()