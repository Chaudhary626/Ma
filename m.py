# main_bot.py (Final Corrected & Professional Version)
import sqlite3
import logging
import random
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
)
from telegram.error import Forbidden

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_IDS = [5718213826]
DB_NAME = "engagement_bot_final.db"
MAX_VIDEOS_PER_USER = 5
STRIKE_LIMIT = 4
VERIFICATION_EXPIRY_HOURS = 4
MIN_RATINGS_FOR_FLAG = 5
QUALITY_SCORE_FLAG_THRESHOLD = 40.0

# --- Logging ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Conversation States ---
# At the top of your file, with the other states
(
    AWAIT_TITLE, AWAIT_THUMBNAIL, AWAIT_DURATION, AWAIT_LINK,
    AWAIT_TASK_PROOF, AWAIT_REJECTION_REASON,
    AWAIT_PAYMENT_PRICE, AWAIT_PAYMENT_INSTRUCTIONS, AWAIT_PAYMENT_PHOTO,
    AWAIT_PAYMENT_PROOF,
    AWAIT_SUBSCRIPTION_PROOF, AWAIT_TRIAL_DAYS,
    # Add these two new states
    AWAIT_REPORT_USER_ID, AWAIT_REPORT_REASON,
    AWAIT_APPEAL_REASON,
    
) = range(15)


# --- Database Setup ---
def initialize_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active',
        strikes INTEGER NOT NULL DEFAULT 0, wants_next_task BOOLEAN NOT NULL DEFAULT 1,
        completed_tasks INTEGER NOT NULL DEFAULT 0, tier TEXT NOT NULL DEFAULT 'Bronze',
        has_paid BOOLEAN NOT NULL DEFAULT 0, credits INTEGER NOT NULL DEFAULT 0
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        video_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL,
        thumbnail_file_id TEXT NOT NULL, duration INTEGER NOT NULL, link TEXT,
        upload_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'active',
        views_received INTEGER NOT NULL DEFAULT 0, quality_score REAL NOT NULL DEFAULT 100.0,
        total_ratings INTEGER NOT NULL DEFAULT 0, FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        task_id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL, uploader_id INTEGER NOT NULL,
        viewer_id INTEGER NOT NULL, status TEXT NOT NULL, proof_file_id TEXT,
        assigned_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, proof_timestamp DATETIME, rejection_reason TEXT,
        quality_rating INTEGER
    )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS watched_videos (user_id INTEGER NOT NULL, video_id INTEGER NOT NULL, PRIMARY KEY (user_id, video_id))")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reciprocal_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owed_by_user_id INTEGER NOT NULL,
        owed_to_user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    default_settings = {
        'payment_required': '0', 'payment_price': '50 INR',
        'payment_instructions': 'Please pay to UPI ID: your-upi@id', 'payment_photo_id': None,
        'reciprocal_tasks_enabled': '1', 'quality_score_enabled': '1',
        'task_credits_enabled': '1', 'unique_transaction_id_enabled': '1'
    }
    for key, value in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, str(value) if value is not None else None))
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully.")

# --- Helper Functions ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS

#
# DELETE your old check_user_access function and REPLACE it with this block
#
# Replace your check_user_access function with this new version

def check_user_access(user_id: int) -> bool:
    conn = get_db_connection()

    # 1. NEW: Check for blocked status first
    user_status = conn.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if user_status and user_status['status'] == 'blocked':
        conn.close()
        return False

    # Check if payment is required at all
    payment_required_setting = conn.execute("SELECT value FROM settings WHERE key = 'payment_required'").fetchone()
    if not (payment_required_setting and payment_required_setting['value'] == '1'):
        conn.close()
        return True

    # Fetch user data
    user = conn.execute("SELECT has_paid, trial_start_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return False

    # Check for active subscription (has_paid flag)
    if user['has_paid']:
        conn.close()
        return True

    # Check for active free trial if not paid
    free_trial_hours_setting = conn.execute("SELECT value FROM settings WHERE key = 'free_trial_days'").fetchone()
    free_trial_hours = int(free_trial_hours_setting['value']) if free_trial_hours_setting else 0
    
    if free_trial_hours > 0 and user['trial_start_date']:
        try:
            start_date = datetime.strptime(user['trial_start_date'], '%Y-%m-%d %H:%M:%S')
            if datetime.now() <= start_date + timedelta(hours=free_trial_hours):
                conn.close()
                return True
        except (ValueError, TypeError):
            pass
            
    # If neither paid, in trial, nor unblocked, deny access
    conn.close()
    return False

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

# --- Payment & Onboarding ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    user_exists = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user.id,)).fetchone()

    # âœ… NEW: Add trial start date and subscription status for new users
    if not user_exists:
        current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            "INSERT INTO users (user_id, trial_start_date, subscription_status) VALUES (?, ?, ?)",
            (user.id, current_time_str, 'trial')
        )
        conn.commit()
        logger.info(f"New user registered with trial: {user.id}")
    
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    user_has_paid = conn.execute("SELECT has_paid FROM users WHERE user_id = ?", (user.id,)).fetchone()['has_paid']
    conn.close()

    # The rest of the function remains the same, access is now controlled by the updated `check_user_access`
    if settings.get('payment_required') == '1' and not user_has_paid:
        # Check trial again before showing payment message
        if check_user_access(user.id):
             welcome_message = (f"ðŸš€ *Welcome, {user.first_name}!* (Final Version)\n\n" "This bot uses a fair, reciprocal exchange system to help you grow your channel.\n\n" "You are currently on a free trial. Use /trialstatus to check its duration.\n\n" "Type /menu to see all available commands.")
             await update.message.reply_text(welcome_message, parse_mode='Markdown')
             return

        tx_id_info = ""
        if settings.get('unique_transaction_id_enabled') == '1':
            tx_id = f"TX-{random.randint(10000, 99999)}"
            context.user_data['tx_id'] = tx_id
            tx_id_info = f"\n\n*IMPORTANT*: Please include this code in your payment notes/remarks: `{tx_id}`"

        payment_caption = (f"ðŸ‘‹ *Welcome, {user.first_name}!* To use this bot, a one-time payment is required.\n\n" f"ðŸ’° **Amount:** {settings.get('payment_price')}\n{tx_id_info}\n\n" f"ðŸ“ **Instructions:**\n{settings.get('payment_instructions')}\n\n" "After paying, press the button below to submit your proof.")
        keyboard = [[InlineKeyboardButton("âœ… Submit Payment Proof", callback_data="submit_payment_proof")]]
        photo_id = settings.get('payment_photo_id')
        if photo_id:
            await update.message.reply_photo(photo=photo_id, caption=payment_caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            await update.message.reply_text(payment_caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return
    else:
        welcome_message = (f"ðŸš€ *Welcome back, {user.first_name}!* (Final Version)\n\n" "This bot uses a fair, reciprocal exchange system to help you grow your channel.\n\n" "Type /menu to see all available commands.")
        await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def submit_payment_proof_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Please upload a screenshot of your successful payment now.")
    return AWAIT_PAYMENT_PROOF

async def received_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("That's not a photo. Please upload a payment screenshot.")
        return AWAIT_PAYMENT_PROOF
    user = update.effective_user
    proof_photo_id = update.message.photo[-1].file_id
    tx_id_info = f" (TX ID: `{context.user_data.get('tx_id', 'N/A')}`)" if context.user_data.get('tx_id') else ""
    await update.message.reply_text("âœ… Thank you! Your proof has been submitted. Admins will verify it shortly.\n\nYou can check your status with the /approve command.")
    notification_caption = (f"ðŸ”” *Payment Verification Required*\n\n" f"User *{user.first_name}* (ID: `{user.id}`){tx_id_info} has submitted the attached payment proof.\n\n" f"Please verify and use `/approve {user.id}` to grant access.")
    for admin_id in ADMIN_IDS:
        try: await context.bot.send_photo(chat_id=admin_id, photo=proof_photo_id, caption=notification_caption, parse_mode='Markdown')
        except Forbidden: logger.warning(f"Could not send payment proof to admin {admin_id}")
    context.user_data.pop('tx_id', None)
    return ConversationHandler.END

# --- Command Access Wrapper ---
async def command_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, command_func, is_conv_starter=False):
    if not check_user_access(update.effective_user.id):
        # âœ… NEW: Custom message for expired trial users
        conn = get_db_connection()
        user = conn.execute("SELECT trial_start_date FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()
        free_trial_days_setting = conn.execute("SELECT value FROM settings WHERE key = 'free_trial_days'").fetchone()
        conn.close()
        
        trial_has_run = user and user['trial_start_date'] and free_trial_days_setting and int(free_trial_days_setting['value']) > 0

        if trial_has_run:
            await update.message.reply_text("â³ Your free trial has ended. To continue using the bot, please subscribe.\n\nUse /pay to see payment instructions.")
        else:
            await start_command(update, context)
            
        if is_conv_starter: return ConversationHandler.END
        return
    return await command_func(update, context)

# --- USER COMMANDS ---
async def approve_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if check_user_access(update.effective_user.id):
        await update.message.reply_text("âœ… Your account is fully approved and active.")
    else:
        await update.message.reply_text("â³ Your payment is still pending verification by an admin. Please be patient.")

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ðŸ“œ *Bot Rules & Guidelines*\n\n1. *Direct Exchange*: After you approve a task, that user is prioritized to watch your video.\n2. *Fair Play*: Honest engagement is required.\n3. *Video Limit*: Max 5 videos (5 min max duration).\n4. *Proof*: A full screen recording is mandatory.\n5. *Strike System*: 4 strikes = temporary ban.", parse_mode='Markdown')

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("âž• Upload Video", callback_data="start_upload")], [InlineKeyboardButton("âœ… Get a Task", callback_data="get_task")], [InlineKeyboardButton("ðŸ“Š My Status", callback_data="my_status")], [InlineKeyboardButton("ðŸ—‘ï¸ Remove Video", callback_data="remove_video_start")], [InlineKeyboardButton("ðŸ§¾ Submit Task Proof", callback_data="submit_task_proof")]]
    await update.message.reply_text("ðŸ“‹ Menu:", reply_markup=InlineKeyboardMarkup(keyboard))

async def my_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    user_info = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    videos = conn.execute("SELECT title, views_received, quality_score FROM videos WHERE user_id = ?", (user_id,)).fetchall()
    owed_tasks = conn.execute("SELECT COUNT(*) FROM reciprocal_tasks WHERE owed_by_user_id = ? AND status = 'pending'", (user_id,)).fetchone()[0]
    pending_verifications = conn.execute("SELECT COUNT(*) FROM tasks WHERE uploader_id = ? AND status = 'proof_submitted'", (user_id,)).fetchone()[0]
    conn.close()
    credit_info = f"ðŸ’° Credits: *{user_info['credits']}*\n" if settings.get('task_credits_enabled') == '1' else ""
    status_message = (f"ðŸ“Š *Your Status*\n\n" f"ðŸ… Tier: *{user_info['tier']}*\n" f"âœ… Tasks Completed: *{user_info['completed_tasks']}*\n" f"ðŸ”¥ Strikes: *{user_info['strikes']} / {STRIKE_LIMIT}*\n" f"{credit_info}" f"ðŸ¤ Direct Exchanges Owed: *{owed_tasks}*\n" f"â³ Tasks Pending Your Verification: *{pending_verifications}*\n\n" f"ðŸ“š *Your Videos ({len(videos)}/{MAX_VIDEOS_PER_USER})*\n")
    if not videos: status_message += "_No videos uploaded._"
    else:
        for i, video in enumerate(videos): status_message += f"{i+1}. `{video['title'][:45]}` (Views: {video['views_received']}" + (f", Quality: {video['quality_score']:.0f}%)" if settings.get('quality_score_enabled') == '1' else ")") + "\n"
    await update.message.reply_text(status_message, parse_mode='Markdown')

async def toggle_participation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    action = 'pause' if update.message.text == '/close' else 'resume'
    conn = get_db_connection()
    if action == 'pause':
        conn.execute("UPDATE users SET status = 'paused' WHERE user_id = ?", (user_id,))
        message = "â¸ï¸ Your participation has been *paused*. You will not receive new tasks. Use /open to resume."
    else:
        conn.execute("UPDATE users SET status = 'active' WHERE user_id = ?", (user_id,))
        message = "â–¶ï¸ Your participation has been *resumed*! You are now eligible for tasks."
    conn.commit()
    conn.close()
    await update.message.reply_text(message, parse_mode='Markdown')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    top_users = conn.execute("SELECT user_id, completed_tasks FROM users ORDER BY completed_tasks DESC LIMIT 10").fetchall()
    conn.close()
    leaderboard_text = "ðŸ† *Top 10 Users*\n\n"
    if not top_users: leaderboard_text += "No users have completed tasks yet."
    else:
        for i, user in enumerate(top_users):
            try:
                member = await context.bot.get_chat(user['user_id'])
                name = member.first_name
            except Exception: name = f"User ID {user['user_id']}"
            leaderboard_text += f"*{i+1}.* {name} - {user['completed_tasks']} tasks\n"
    await update.message.reply_text(leaderboard_text, parse_mode='Markdown')

async def remove_video_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_sender = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    conn = get_db_connection()
    videos = conn.execute("SELECT video_id, title FROM videos WHERE user_id = ?", (user_id,)).fetchall()
    conn.close()
    if not videos:
        await message_sender.reply_text("You have no videos to remove.")
        return
    keyboard = [[InlineKeyboardButton(f"ðŸ—‘ï¸ {v['title'][:40]}", callback_data=f"remove_confirm_{v['video_id']}")] for v in videos]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="remove_cancel")])
    await message_sender.reply_text("Select a video to remove:", reply_markup=InlineKeyboardMarkup(keyboard))

async def remove_video_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    video_id = int(query.data.split('_')[2])
    conn = get_db_connection()
    video = conn.execute("SELECT status FROM videos WHERE video_id = ? AND user_id = ?", (video_id, query.from_user.id)).fetchone()
    if not video:
        await query.edit_message_text("Error: Video not found.")
    elif video['status'] == 'being_watched':
        await query.edit_message_text("âš ï¸ This video cannot be removed as it's being processed.")
    else:
        conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM tasks WHERE video_id = ?", (video_id,))
        conn.commit()
        await query.edit_message_text("âœ… Video has been successfully removed.")
    conn.close()

# --- TASK MANAGEMENT ---
async def get_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_sender = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    if conn.execute("SELECT 1 FROM tasks WHERE viewer_id = ? AND status IN ('assigned', 'proof_submitted')", (user_id,)).fetchone():
        await message_sender.reply_text("You already have an active task.")
        conn.close()
        return
    if settings.get('task_credits_enabled') == '1':
        user_credits = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()['credits']
        if user_credits <= 0:
            await message_sender.reply_text("âš ï¸ You have no credits! Complete more tasks to earn credits for your own videos.")
            conn.close()
            return
    video_to_watch, reciprocal_task_id = None, None
    if settings.get('reciprocal_tasks_enabled') == '1':
        reciprocal_obligation = conn.execute("SELECT id, owed_to_user_id FROM reciprocal_tasks WHERE owed_by_user_id = ? AND status = 'pending' ORDER BY created_timestamp ASC LIMIT 1", (user_id,)).fetchone()
        if reciprocal_obligation:
            video_to_watch = conn.execute("SELECT v.*, 'Bronze' as tier FROM videos v WHERE v.user_id = ? AND v.status = 'active' AND v.video_id NOT IN (SELECT video_id FROM watched_videos WHERE user_id = ?) ORDER BY RANDOM() LIMIT 1", (reciprocal_obligation['owed_to_user_id'], user_id)).fetchone()
            if video_to_watch: reciprocal_task_id = reciprocal_obligation['id']
    if not video_to_watch:
        video_to_watch = conn.execute("SELECT v.*, u.tier FROM videos v JOIN users u ON v.user_id = u.user_id LEFT JOIN watched_videos wv ON v.video_id = wv.video_id AND wv.user_id = ? WHERE v.user_id != ? AND v.status = 'active' AND wv.video_id IS NULL ORDER BY CASE u.tier WHEN 'Gold' THEN 3 WHEN 'Silver' THEN 2 ELSE 1 END DESC, v.views_received ASC, RANDOM() LIMIT 1", (user_id, user_id)).fetchone()
    if not video_to_watch:
        await message_sender.reply_text("No new videos available right now.")
        conn.close()
        return
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (video_id, uploader_id, viewer_id, status) VALUES (?, ?, ?, 'assigned')", (video_to_watch['video_id'], video_to_watch['user_id'], user_id))
    cursor.execute("UPDATE videos SET status = 'being_watched' WHERE video_id = ?", (video_to_watch['video_id'],))
    task_type_info = "This is a direct exchange task." if reciprocal_task_id else f"Uploader Tier: {video_to_watch['tier']}"
    if reciprocal_task_id: cursor.execute("UPDATE reciprocal_tasks SET status = 'completed' WHERE id = ?", (reciprocal_task_id,))
    if settings.get('task_credits_enabled') == '1':
        conn.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (video_to_watch['duration'], video_to_watch['user_id']))
    conn.commit()
    conn.close()
    task_message = (f"ðŸ”¥ *New Task Assigned!* ({task_type_info})\n\n" f"_*Instructions:*_\n" f"1. Search YouTube for: `{video_to_watch['title']}`\n" f"2. Find the video with this thumbnail.\n" f"3. Watch at least *{max(1, video_to_watch['duration'] // 2)} minute(s)*.\n" f"4. Like, Comment, and Subscribe.\n\n" f"When done, use /submitproof.")
    await message_sender.reply_photo(photo=video_to_watch['thumbnail_file_id'], caption=task_message, parse_mode='Markdown')

# --- CONVERSATIONS ---

#
# ADD this new appeal conversation code block
#
async def appeal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the appeal conversation."""
    query = update.callback_query
    await query.answer()

    report_id = int(query.data.split('_')[2])
    context.user_data['appeal_report_id'] = report_id

    await query.message.reply_text(
        "You have chosen to appeal this report. "
        "Please state your case clearly. Your response will be sent to the admins."
    )
    return AWAIT_APPEAL_REASON

async def received_appeal_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the appeal reason and notifies the admin."""
    appeal_reason = update.message.text
    report_id = context.user_data.get('appeal_report_id')
    appealing_user_id = update.effective_user.id

    conn = get_db_connection()
    conn.execute(
        "UPDATE reports SET appeal_reason = ?, status = 'appealed', appeal_timestamp = CURRENT_TIMESTAMP WHERE report_id = ?",
        (appeal_reason, report_id)
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(" Your appeal has been submitted and will be reviewed by an admin.")

    # Notify admin of the appeal
    for admin_id in ADMIN_IDS:
        try:
            admin_message = (
                f" Report Appeal Filed for Report #{report_id}\n\n"
                f"User `{appealing_user_id}` has appealed.\n\n"
                f"*Their appeal:* {appeal_reason}\n\n"
                f"Use /viewreports to see the original report and this appeal."
            )
            await context.bot.send_message(chat_id=admin_id, text=admin_message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to send appeal notification to admin {admin_id}: {e}")

    context.user_data.clear()
    return ConversationHandler.END

#
# ADD this new report conversation code block
#

async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the user reporting conversation."""
    await update.message.reply_text("You are about to file a report. Who is the user you would like to report? Please provide their User ID.")
    return AWAIT_REPORT_USER_ID

async def received_report_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the ID of the user to be reported."""
    try:
        reported_user_id = int(update.message.text)
        context.user_data['reported_user_id'] = reported_user_id
        await update.message.reply_text("Thank you. Please describe the reason for your report.")
        return AWAIT_REPORT_REASON
    except ValueError:
        await update.message.reply_text("That is not a valid User ID. Please provide a number. You can use a User ID finder bot to get a user's ID.")
        return AWAIT_REPORT_USER_ID

#
# REPLACE your existing received_report_reason function with this one
#
async def received_report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the reason, saves the report, and notifies all parties."""
    reason = update.message.text
    reporter_id = update.effective_user.id
    reported_user_id = context.user_data.get('reported_user_id')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO reports (reporter_id, reported_user_id, reason) VALUES (?, ?, ?)",
        (reporter_id, reported_user_id, reason)
    )
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await update.message.reply_text(" Your report has been filed. Thank you.")
    
    # Notify admin
    for admin_id in ADMIN_IDS:
        try:
            # This message is now more robust to prevent formatting errors
            admin_message = " New User Report Filed.\n"
            admin_message += f"Report ID: #{report_id}\n\n"
            admin_message += "Use /viewreports to see details."
            await context.bot.send_message(chat_id=admin_id, text=admin_message)
        except Exception as e:
            logger.error(f"Failed to send report notification to admin {admin_id}: {e}")

    # Notify the reported user and give them an appeal option
    try:
        reported_user_message = (
            f" You have been reported by user `{reporter_id}`.\n\n"
            f"*Reason:* {reason}\n\n"
            f"If you believe this report is incorrect, you have the right to appeal."
        )
        keyboard = [[InlineKeyboardButton("Appeal This Report", callback_data=f"appeal_report_{report_id}")]]
        await context.bot.send_message(
            chat_id=reported_user_id,
            text=reported_user_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send report notification to reported user {reported_user_id}: {e}")
            
    context.user_data.clear()
    return ConversationHandler.END

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message_sender = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    conn = get_db_connection()
    video_count = conn.execute("SELECT COUNT(*) FROM videos WHERE user_id = ?", (user_id,)).fetchone()[0]
    conn.close()
    if video_count >= MAX_VIDEOS_PER_USER:
        await message_sender.reply_text(f"âš ï¸ You have reached the max of {MAX_VIDEOS_PER_USER} videos.")
        return ConversationHandler.END
    context.user_data['video_info'] = {}
    await message_sender.reply_text("Enter the *exact title* of your YouTube video:", parse_mode='Markdown')
    return AWAIT_TITLE

async def received_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['video_info']['title'] = update.message.text
    await update.message.reply_text("Send the *thumbnail* as a photo.", parse_mode='Markdown')
    return AWAIT_THUMBNAIL

async def received_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("That's not a photo. Please send an image.")
        return AWAIT_THUMBNAIL
    context.user_data['video_info']['thumbnail_file_id'] = update.message.photo[-1].file_id
    await update.message.reply_text("Enter the video's duration in *minutes* (1-5).", parse_mode='Markdown')
    return AWAIT_DURATION

async def received_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        duration = int(update.message.text)
        if not 1 <= duration <= 5:
            await update.message.reply_text("Duration must be between 1 and 5.")
            return AWAIT_DURATION
        context.user_data['video_info']['duration'] = duration
        await update.message.reply_text("Enter a direct link (optional, type 'skip' if not).")
        return AWAIT_LINK
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return AWAIT_DURATION

async def received_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    context.user_data['video_info']['link'] = None if link.lower() == 'skip' else link
    user_id = update.effective_user.id
    video = context.user_data['video_info']
    conn = get_db_connection()
    conn.execute("INSERT INTO videos (user_id, title, thumbnail_file_id, duration, link) VALUES (?, ?, ?, ?, ?)", (user_id, video['title'], video['thumbnail_file_id'], video['duration'], video['link']))
    conn.commit()
    conn.close()
    await update.message.reply_text("âœ… *Video uploaded successfully!*", parse_mode='Markdown')
    context.user_data.clear()
    return ConversationHandler.END

async def submit_task_proof_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    task = conn.execute("SELECT task_id FROM tasks WHERE viewer_id = ? AND status = 'assigned'", (update.effective_user.id,)).fetchone()
    conn.close()
    if not task:
        await update.message.reply_text("You don't have an active task.")
        return ConversationHandler.END
    context.user_data['task_id_for_proof'] = task['task_id']
    await update.message.reply_text("Upload your screen recording video as proof.")
    return AWAIT_TASK_PROOF

async def received_task_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("That's not a video. Please upload a screen recording.")
        return AWAIT_TASK_PROOF
    task_id, proof_file_id = context.user_data.get('task_id_for_proof'), update.message.video.file_id
    conn = get_db_connection()
    conn.execute("UPDATE tasks SET proof_file_id = ?, status = 'proof_submitted', proof_timestamp = CURRENT_TIMESTAMP WHERE task_id = ?", (proof_file_id, task_id))
    conn.commit()
    task_data = conn.execute("SELECT uploader_id, viewer_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    await update.message.reply_text("âœ… Task proof submitted for verification.")
    verification_message = f"ðŸ”” *Task Verification Required*\n\nUser `{task_data['viewer_id']}` submitted proof."
    keyboard = [[InlineKeyboardButton("âœ… Accept", callback_data=f"verify_accept_{task_id}"), InlineKeyboardButton("âŒ Reject", callback_data=f"verify_reject_{task_id}")]]
    try: await context.bot.send_video(chat_id=task_data['uploader_id'], video=proof_file_id, caption=verification_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except Forbidden: pass
    context.user_data.clear()
    return ConversationHandler.END

async def handle_verification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, user_id = update.callback_query, update.effective_user.id
    await query.answer()
    action, task_id = query.data.split('_')[1], int(query.data.split('_')[2])
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    task = conn.execute("SELECT t.*, v.duration FROM tasks t JOIN videos v ON t.video_id = v.video_id WHERE t.task_id = ? AND t.uploader_id = ? AND t.status = 'proof_submitted'", (task_id, user_id)).fetchone()
    if not task:
        await query.edit_message_text("Task already processed.")
        conn.close()
        return
    if action == "accept":
        video_id, viewer_id, uploader_id = task['video_id'], task['viewer_id'], task['uploader_id']
        cursor = conn.cursor()
        cursor.execute("UPDATE tasks SET status = 'completed' WHERE task_id = ?", (task_id,))
        cursor.execute("UPDATE users SET completed_tasks = completed_tasks + 1 WHERE user_id = ?", (viewer_id,))
        if settings.get('task_credits_enabled') == '1':
            credits_earned = task['duration']
            cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (credits_earned, viewer_id))
        cursor.execute("INSERT OR IGNORE INTO watched_videos (user_id, video_id) VALUES (?, ?)", (viewer_id, video_id))
        cursor.execute("UPDATE videos SET views_received = views_received + 1, status = 'active' WHERE video_id = ?", (video_id,))
        if settings.get('reciprocal_tasks_enabled') == '1': cursor.execute("INSERT INTO reciprocal_tasks (owed_by_user_id, owed_to_user_id) VALUES (?, ?)", (uploader_id, viewer_id))
        conn.commit()
        await query.edit_message_caption(caption="âœ… *Proof Accepted!*\nA reciprocal task has been created.", parse_mode='Markdown')
        try:
            await context.bot.send_message(chat_id=viewer_id, text="ðŸŽ‰ Your proof was accepted!")
            if settings.get('quality_score_enabled') == '1':
                keyboard = [[InlineKeyboardButton("ðŸ‘ Good Video", callback_data=f"rate_good_{video_id}_{task_id}"), InlineKeyboardButton("ðŸ‘Ž Bad Video", callback_data=f"rate_bad_{video_id}_{task_id}")]]
                await context.bot.send_message(chat_id=viewer_id, text="Finally, please rate the quality of the video you just watched.", reply_markup=InlineKeyboardMarkup(keyboard))
        except Forbidden: pass
    elif action == "reject":
        context.user_data['rejection_info'] = {'task_id': task_id, 'viewer_id': task['viewer_id']}
        await query.edit_message_caption(caption="*Proof Rejection*\nPlease provide a brief reason.", parse_mode='Markdown')
        return AWAIT_REJECTION_REASON
    conn.close()

async def received_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason, info = update.message.text, context.user_data.get('rejection_info')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET status = 'failed', rejection_reason = ? WHERE task_id = ?", (reason, info['task_id']))
    cursor.execute("UPDATE users SET strikes = strikes + 1 WHERE user_id = ?", (info['viewer_id'],))
    video_id = cursor.execute("SELECT video_id FROM tasks WHERE task_id = ?", (info['task_id'],)).fetchone()['video_id']
    cursor.execute("UPDATE videos SET status = 'active' WHERE video_id = ?", (video_id,))
    conn.commit()
    new_strikes = conn.execute("SELECT strikes FROM users WHERE user_id = ?", (info['viewer_id'],)).fetchone()['strikes']
    conn.close()
    await update.message.reply_text("Rejection recorded.")
    try: await context.bot.send_message(chat_id=info['viewer_id'], text=f"âŒ Your proof was rejected.\n*Reason*: {reason}\nYou now have *{new_strikes}* strike(s).", parse_mode='Markdown')
    except Forbidden: pass
    context.user_data.clear()
    return ConversationHandler.END

async def rate_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, rating_type, video_id_str, task_id_str = query.data.split('_')
    video_id, task_id = int(video_id_str), int(task_id_str)
    rating_value = 1 if rating_type == "good" else 0
    conn = get_db_connection()
    task = conn.execute("SELECT quality_rating FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if task and task['quality_rating'] is not None:
        await query.edit_message_text("You have already rated this video. Thank you!")
        conn.close()
        return
    conn.execute("UPDATE tasks SET quality_rating = ? WHERE task_id = ?", (rating_value, task_id))
    video_info = conn.execute("SELECT quality_score, total_ratings FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    new_total_ratings = video_info['total_ratings'] + 1
    new_quality_score = ((video_info['quality_score'] * video_info['total_ratings']) + (rating_value * 100)) / new_total_ratings
    conn.execute("UPDATE videos SET quality_score = ?, total_ratings = ? WHERE video_id = ?", (new_quality_score, new_total_ratings, video_id))
    await query.edit_message_text("Thank you for your feedback!")
    if new_total_ratings >= MIN_RATINGS_FOR_FLAG and new_quality_score < QUALITY_SCORE_FLAG_THRESHOLD:
        conn.execute("UPDATE videos SET status = 'flagged' WHERE video_id = ?", (video_id,))
        video_title = conn.execute("SELECT title FROM videos WHERE video_id = ?", (video_id,)).fetchone()['title']
        for admin_id in ADMIN_IDS:
            try: await context.bot.send_message(chat_id=admin_id, text=f"âš ï¸ *Video Flagged*\n\nVideo `{video_title}` (ID: {video_id}) has been automatically flagged for low quality ({new_quality_score:.0f}%) and paused.")
            except Forbidden: pass
    conn.commit()
    conn.close()

# --- ADMIN ---

#
async def my_reports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows a user to see the status of their own reports."""
    user_id = update.effective_user.id
    conn = get_db_connection()

    reports_filed = conn.execute("SELECT report_id, reported_user_id, status FROM reports WHERE reporter_id = ? ORDER BY timestamp DESC", (user_id,)).fetchall()
    reports_against = conn.execute("SELECT report_id, reporter_id, status FROM reports WHERE reported_user_id = ? ORDER BY timestamp DESC", (user_id,)).fetchall()
    conn.close()

    message = " *Your Report Summary*\n\n"
    message += "*Reports You Have Filed:*\n"
    if not reports_filed:
        message += "_You have not filed any reports._\n"
    else:
        for report in reports_filed:
            message += f"- Report `#{report['report_id']}` against `{report['reported_user_id']}` (Status: *{report.get('status', 'filed')}*)\n"

    message += "\n*Reports Filed Against You:*\n"
    if not reports_against:
        message += "_No reports have been filed against you._\n"
    else:
        for report in reports_against:
            message += f"- Reported by `{report['reporter_id']}` (Status: *{report.get('status', 'filed')}*)\n"

    await update.message.reply_text(message, parse_mode='Markdown')

#
# Make sure this is the admin_view_reports function in your file
#
async def admin_view_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows an admin to view the latest user reports and any appeals."""
    if not is_admin(update.effective_user.id): return

    conn = get_db_connection()
    try:
        reports = conn.execute("SELECT * FROM reports ORDER BY timestamp DESC LIMIT 10").fetchall()
    except sqlite3.OperationalError:
        await update.message.reply_text("Error: The 'reports' table seems to be missing. Please delete your .db file and restart the bot.")
        conn.close()
        return
        
    conn.close()

    if not reports:
        await update.message.reply_text("No user reports have been filed yet.")
        return

    message = " *Most Recent User Reports:*\n"
    message += "--------------------------------\n"
    for report in reports:
        try:
            report_id = report['report_id']
            status = report.get('status', 'filed')
            appeal_reason = report.get('appeal_reason')
            
            message += (
                f" *Report #{report_id}* (Status: {status})\n"
                f"   - *From User:* `{report['reporter_id']}`\n"
                f"   - *Against User:* `{report['reported_user_id']}`\n"
                f"   - *Reason:* {report.get('reason', 'N/A')}\n"
            )
            
            if status == 'appealed' and appeal_reason:
                message += f"   - *Appeal Reason:* {appeal_reason}\n"
            
            message += "--------------------------------\n"
        except Exception as e:
            logger.error(f"Could not format report #{report.get('report_id')}: {e}")
            message += f" Error displaying Report #{report.get('report_id')}.\n--------------------------------\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

#
# ADD these two new functions to your admin section
#

# REPLACE your existing admin_user_management_panel function
async def admin_user_management_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user management sub-panel."""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("ðŸ”’ Block User", callback_data="instruct_block"), InlineKeyboardButton("ðŸ”“ Unblock User", callback_data="instruct_unblock")],
        [InlineKeyboardButton("âš¡ï¸ Add Strike", callback_data="instruct_addstrike"), InlineKeyboardButton("âœ¨ Remove Strike", callback_data="instruct_removestrike")],
        [InlineKeyboardButton("ðŸ“ Pending Proofs", callback_data="instruct_pendingproofs")],
        # This is the new button
        [InlineKeyboardButton("ðŸ“„ Review Reports", callback_data="instruct_viewreports")],
        [InlineKeyboardButton("Â« Back to Main Panel", callback_data="admin_main_panel")]
    ]
    
    await query.edit_message_text(
        text="ðŸ‘¨â€âš–ï¸ *User Management Panel*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

# REPLACE your existing admin_show_command_instructions function
async def admin_show_command_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies with usage instructions for admin commands."""
    query = update.callback_query
    await query.answer()
    
    command_map = {
        "instruct_block": "To block a user, type:\n`/block <user_id>`",
        "instruct_unblock": "To unblock a user, type:\n`/unblock <user_id>`",
        "instruct_addstrike": "To add a strike, type:\n`/addstrike <user_id>`",
        "instruct_removestrike": "To remove a strike, type:\n`/removestrike <user_id>`",
        "instruct_pendingproofs": "To see users with pending proofs, type:\n`/pendingproofs`",
        # This is the new instruction
        "instruct_viewreports": "To see the latest user reports, type:\n`/viewreports`",
    }
    
    instruction_text = command_map.get(query.data, "Unknown command.")
    await query.message.reply_text(instruction_text, parse_mode='Markdown')

#
# REPLACE your existing admin_panel_command function with this one
#
async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    
    keyboard = [
        [InlineKeyboardButton("ðŸ’³ Payment Settings", callback_data="admin_payment_settings")],
        [InlineKeyboardButton("âš™ï¸ Feature Settings", callback_data="admin_feature_settings")],
        # This is the new button we are adding
        [InlineKeyboardButton("ðŸ‘¨â€âš–ï¸ User Management", callback_data="admin_user_management")]
    ]
    
    # Use edit_message_text if coming from a callback, otherwise reply
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("ðŸ‘‘ *Admin Panel*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text("ðŸ‘‘ *Admin Panel*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# âœ… REPLACEMENT for the admin_payment_settings_panel function

async def admin_payment_settings_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    conn.close()
    payment_status = "âœ… Enabled" if settings.get('payment_required') == '1' else "âŒ Disabled"
    photo_status = "âœ… Set" if settings.get('payment_photo_id') else "âŒ Not Set"
    tx_id_status = "âœ… Enabled" if settings.get('unique_transaction_id_enabled') == '1' else "âŒ Disabled"
    
    keyboard = [
        [InlineKeyboardButton(f"Require Payments: {payment_status}", callback_data="admin_toggle_payment")],
        # The buttons are now clearer and don't conflict
        [InlineKeyboardButton("Set Subscription Price", callback_data="admin_set_price"), InlineKeyboardButton("Set UPI ID", callback_data="admin_set_upi")],
        [InlineKeyboardButton(f"Payment Photo: {photo_status}", callback_data="admin_set_photo"), InlineKeyboardButton("Remove Photo", callback_data="admin_remove_photo")],
        [InlineKeyboardButton(f"Unique TX ID: {tx_id_status}", callback_data="admin_toggle_tx")],
        [InlineKeyboardButton("Â« Back to Main Panel", callback_data="admin_main_panel")]
    ]
    await query.message.edit_text("ðŸ’³ *Payment Settings*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_feature_settings_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    conn.close()
    reciprocal_status = "âœ… Enabled" if settings.get('reciprocal_tasks_enabled') == '1' else "âŒ Disabled"
    quality_status = "âœ… Enabled" if settings.get('quality_score_enabled') == '1' else "âŒ Disabled"
    credits_status = "âœ… Enabled" if settings.get('task_credits_enabled') == '1' else "âŒ Disabled"
    keyboard = [[InlineKeyboardButton(f"Reciprocal Tasks: {reciprocal_status}", callback_data="admin_toggle_reciprocal")], [InlineKeyboardButton(f"Video Quality Score: {quality_status}", callback_data="admin_toggle_quality")], [InlineKeyboardButton(f"Task Credits System: {credits_status}", callback_data="admin_toggle_credits")], [InlineKeyboardButton("Â« Back to Main Panel", callback_data="admin_main_panel")]]
    await query.message.edit_text("âš™ï¸ *Feature Settings*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_toggle_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    setting_key = query.data.split('_')[-1]
    db_key_map = {'payment': 'payment_required', 'tx': 'unique_transaction_id_enabled', 'reciprocal': 'reciprocal_tasks_enabled', 'quality': 'quality_score_enabled', 'credits': 'task_credits_enabled'}
    db_key = db_key_map.get(setting_key)
    if not db_key: return
    conn = get_db_connection()
    current_val = conn.execute("SELECT value FROM settings WHERE key = ?", (db_key,)).fetchone()['value']
    new_val = '0' if current_val == '1' else '1'
    conn.execute("UPDATE settings SET value = ? WHERE key = ?", (new_val, db_key))
    conn.commit()
    conn.close()
    if "payment" in query.data or "tx" in query.data:
        await admin_payment_settings_panel(update, context)
    else:
        await admin_feature_settings_panel(update, context)

async def admin_approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        user_id = int(context.args[0])
        conn = get_db_connection()
        res = conn.execute("UPDATE users SET has_paid = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        if res.rowcount > 0:
            await update.message.reply_text(f"âœ… Access granted to user `{user_id}`.", parse_mode='Markdown')
            try: await context.bot.send_message(chat_id=user_id, text="ðŸŽ‰ An admin has approved your access! Use /menu to get started.")
            except Forbidden: pass
        else: await update.message.reply_text(f"User `{user_id}` not found.", parse_mode='Markdown')
    except (IndexError, ValueError): await update.message.reply_text("Usage: `/approve <user_id>`")

async def admin_set_price_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Send the new price (e.g., '100 INR').")
    return AWAIT_PAYMENT_PRICE

async def admin_received_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'payment_price'", (update.message.text,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"âœ… Price updated to: {update.message.text}")
    return ConversationHandler.END

async def admin_set_instructions_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Send the new payment instructions (e.g., your UPI ID).")
    return AWAIT_PAYMENT_INSTRUCTIONS

# âœ… REPLACEMENT for the old admin_received_instructions function

async def admin_received_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives and saves the new UPI ID."""
    conn = get_db_connection()
    # This now updates the correct 'upi_id' setting that the /pay command uses
    conn.execute("UPDATE settings SET value = ? WHERE key = 'upi_id'", (update.message.text,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"âœ… UPI ID has been updated to: {update.message.text}")
    return ConversationHandler.END

async def admin_set_photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Please send the photo for payment instructions (e.g., a QR code).")
    return AWAIT_PAYMENT_PHOTO

async def admin_received_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("That's not a photo. Please send an image.")
        return AWAIT_PAYMENT_PHOTO
    photo_id = update.message.photo[-1].file_id
    conn = get_db_connection()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'payment_photo_id'", (photo_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("âœ… Payment photo has been updated successfully.")
    return ConversationHandler.END

async def admin_remove_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    conn = get_db_connection()
    conn.execute("UPDATE settings SET value = NULL WHERE key = 'payment_photo_id'")
    conn.commit()
    conn.close()
    await query.message.reply_text("âœ… Payment photo has been removed.")
    await admin_payment_settings_panel(update, context)

async def admin_approve_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("To approve a user, use the command:\n`/approve <user_id>`\n\nExample:\n`/approve 123456789`", parse_mode='Markdown')

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# âœ… NEW FEATURES ADDED BELOW (AS REQUESTED)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# --- New Feature: Database Additions ---
#
# REPLACE your existing initialize_database_additions function with this one
#
def initialize_database_additions():
    """Safely adds new columns and settings to the database without altering existing data."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Add the new 'reports' table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        report_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER NOT NULL,
        reported_user_id INTEGER NOT NULL,
        reason TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    # The 'try' statement with its correctly indented block
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN trial_start_date TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'none'")
        
        # Add new columns for the appeal system
        cursor.execute("ALTER TABLE reports ADD COLUMN status TEXT DEFAULT 'filed'")
        cursor.execute("ALTER TABLE reports ADD COLUMN appeal_reason TEXT")
        cursor.execute("ALTER TABLE reports ADD COLUMN appeal_timestamp DATETIME")
        
        logger.info("Added new columns to users and reports tables.")
    except sqlite3.OperationalError as e:
        # This handles the case where the columns already exist
        if "duplicate column name" not in str(e):
            raise
        
    # Add new settings for the trial and subscription system
    new_settings = {
        'free_trial_days': '24', # Default to 24 hours
        'subscription_price': '30',
        'upi_id': 'your-upi-id@oksbi'
    }
    for key, value in new_settings.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    
    conn.commit()
    conn.close()
    logger.info("New feature settings initialized successfully.")

#
# Add this entire block of new admin functions to your code
#

async def admin_block_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        user_id_to_block = int(context.args[0])
        conn = get_db_connection()
        conn.execute("UPDATE users SET status = 'blocked' WHERE user_id = ?", (user_id_to_block,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"ðŸ”’ User `{user_id_to_block}` has been blocked.", parse_mode='Markdown')
        await context.bot.send_message(chat_id=user_id_to_block, text="Your account has been blocked by an admin.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/block <user_id>`")
    except Forbidden:
        pass # User may have blocked the bot

async def admin_unblock_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        user_id_to_unblock = int(context.args[0])
        conn = get_db_connection()
        conn.execute("UPDATE users SET status = 'active' WHERE user_id = ?", (user_id_to_unblock,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"ðŸ”“ User `{user_id_to_unblock}` has been unblocked.", parse_mode='Markdown')
        await context.bot.send_message(chat_id=user_id_to_unblock, text="Your account has been unblocked by an admin.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/unblock <user_id>`")
    except Forbidden:
        pass

async def admin_add_strike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        user_id_to_strike = int(context.args[0])
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET strikes = strikes + 1 WHERE user_id = ?", (user_id_to_strike,))
        conn.commit()
        
        new_strikes = cursor.execute("SELECT strikes FROM users WHERE user_id = ?", (user_id_to_strike,)).fetchone()['strikes']
        await update.message.reply_text(f"âš¡ï¸ Strike added. User `{user_id_to_strike}` now has {new_strikes} strike(s).", parse_mode='Markdown')

        if new_strikes >= STRIKE_LIMIT:
            cursor.execute("UPDATE users SET status = 'blocked' WHERE user_id = ?", (user_id_to_strike,))
            conn.commit()
            await update.message.reply_text(f"ðŸš« User `{user_id_to_strike}` has reached the strike limit and has been blocked.", parse_mode='Markdown')
            await context.bot.send_message(chat_id=user_id_to_strike, text=f"You have reached {new_strikes} strikes and your account has been blocked.")

        conn.close()
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/addstrike <user_id>`")
    except Forbidden:
        pass

async def admin_remove_strike(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        user_id_to_pardon = int(context.args[0])
        conn = get_db_connection()
        conn.execute("UPDATE users SET strikes = strikes - 1 WHERE user_id = ? AND strikes > 0", (user_id_to_pardon,))
        conn.commit()
        new_strikes = conn.execute("SELECT strikes FROM users WHERE user_id = ?", (user_id_to_pardon,)).fetchone()['strikes']
        conn.close()
        await update.message.reply_text(f"âœ¨ Strike removed. User `{user_id_to_pardon}` now has {new_strikes} strike(s).", parse_mode='Markdown')
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/removestrike <user_id>`")

async def admin_get_pending_proofs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = get_db_connection()
    pending = conn.execute("""
        SELECT u.user_id, u.first_name, COUNT(t.task_id) as pending_count
        FROM users u
        JOIN tasks t ON u.user_id = t.uploader_id
        WHERE t.status = 'proof_submitted'
        GROUP BY u.user_id
        ORDER BY pending_count DESC
    """).fetchall()
    conn.close()
    
    if not pending:
        await update.message.reply_text("No users have pending proofs to verify.")
        return

    message = "ðŸ“ *Users with Pending Proof Verifications:*\n\n"
    for row in pending:
        message += f"- `{row['user_id']}` ({row['first_name']}): *{row['pending_count']}* proof(s)\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# --- New Feature: UPI Subscription Commands ---
async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user how to pay for a subscription."""
    user_id = update.effective_user.id
    conn = get_db_connection()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    conn.close()
    
    upi_id = settings.get('upi_id', 'your-upi@bank')
    price = settings.get('subscription_price', '30')

    # Termux-safe multi-line f-string using parenthesis
    payment_instruction = (
        f"ðŸ’³ *Subscription Payment Instructions:*\n"
        f"------------------------------------\n"
        f"Send â‚¹{price} to the following UPI ID:\n\n"
        f"`{upi_id}`\n\n"
        f"ðŸ“Œ *VERY IMPORTANT:*\n"
        f"You *must* include the following code in your payment's note/description so we can identify you:\n\n"
        f"`UserID:{user_id}`\n\n"
        f"After paying, use the /submitpaymentproof command to upload a screenshot."
    )

    await update.message.reply_text(payment_instruction, parse_mode='Markdown')

async def submit_payment_proof_convo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the conversation for a user to submit their payment proof."""
    await update.message.reply_text("Please upload the screenshot of your payment now.")
    return AWAIT_SUBSCRIPTION_PROOF

# âœ… REPLACEMENT for the received_subscription_proof function

async def received_subscription_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives and forwards the subscription proof to the admin."""
    if not update.message.photo:
        await update.message.reply_text("That doesn't look like an image. Please send a screenshot.")
        return AWAIT_SUBSCRIPTION_PROOF
        
    user = update.effective_user
    proof_photo_id = update.message.photo[-1].file_id
    
    # This caption is simplified to prevent formatting errors
    caption = (
        f"ðŸ”” Subscription Proof Submitted\n\n"
        f"User: {user.first_name} (ID: {user.id})\n\n"
        f"Please verify the payment and approve or reject the subscription."
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("âœ… Approve Subscription", callback_data=f"sub_approve_{user.id}"),
            InlineKeyboardButton("âŒ Reject Payment", callback_data=f"sub_reject_{user.id}")
        ]
    ])
    
    for admin_id in ADMIN_IDS:
        try:
            # The parse_mode has been removed to ensure reliability
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=proof_photo_id,
                caption=caption,
                reply_markup=keyboard
            )
        except Forbidden:
            logger.warning(f"Could not send subscription proof to admin {admin_id}. Bot might be blocked.")
        except Exception as e:
            logger.error(f"Failed to send proof to admin {admin_id}: {e}")

    await update.message.reply_text("âœ… Thank you! Your proof has been sent to the admins for verification.")
    return ConversationHandler.END

async def handle_subscription_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin's choice to approve or reject a subscription."""
    query = update.callback_query
    await query.answer()
    
    action, user_id_str = query.data.split('_', 2)[1:]
    user_id = int(user_id_str)
    
    conn = get_db_connection()
    if action == "approve":
        # Grant access
        conn.execute("UPDATE users SET has_paid = 1, subscription_status = 'active' WHERE user_id = ?", (user_id,))
        conn.commit()
        
        await query.edit_message_caption(caption=f"âœ… User {user_id} has been approved.", reply_markup=None)
        try:
            await context.bot.send_message(chat_id=user_id, text="ðŸŽ‰ Your subscription has been approved by an admin! You now have full access. Use /menu to get started.")
        except Forbidden:
            logger.warning(f"Could not send approval message to user {user_id}.")

    elif action == "reject":
        await query.edit_message_caption(caption=f"âŒ Payment for user {user_id} was rejected.", reply_markup=None)
        try:
            await context.bot.send_message(chat_id=user_id, text="âš ï¸ Your recent payment proof was rejected by an admin. Please double-check the details and try again, or contact support.")
        except Forbidden:
            logger.warning(f"Could not send rejection message to user {user_id}.")
            
    conn.close()


# --- New Feature: Free Trial Management ---
async def admin_set_trial_days_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to start the process of setting trial days."""
    if not is_admin(update.effective_user.id):
        return # Silently ignore for non-admins
    await update.message.reply_text("Please enter the new number of free trial days (e.g., 7).")
    return AWAIT_TRIAL_DAYS

async def admin_received_trial_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the new trial day count and saves it."""
    try:
        days = int(update.message.text)
        if days < 0:
            await update.message.reply_text("Please enter a non-negative number.")
            return AWAIT_TRIAL_DAYS
            
        conn = get_db_connection()
        conn.execute("UPDATE settings SET value = ? WHERE key = 'free_trial_days'", (str(days),))
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"âœ… Free trial period has been updated to {days} days.")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid number. Please enter a whole number for the days.")
        return AWAIT_TRIAL_DAYS

async def trial_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows a user to check their current trial status."""
    user_id = update.effective_user.id
    conn = get_db_connection()
    user = conn.execute("SELECT has_paid, trial_start_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
    settings = {row['key']: row['value'] for row in conn.execute("SELECT key, value FROM settings").fetchall()}
    conn.close()

    if user['has_paid']:
        await update.message.reply_text("âœ… You have an active subscription and full access to the bot.")
        return

    trial_days = int(settings.get('free_trial_days', 0))
    if trial_days == 0 or not user['trial_start_date']:
        await update.message.reply_text("â„¹ï¸ A free trial is not currently active for your account.\nUse /pay to subscribe.")
        return

    try:
        start_date = datetime.strptime(user['trial_start_date'], '%Y-%m-%d %H:%M:%S')
        expiry_date = start_date + timedelta(days=trial_days)
        time_left = expiry_date - datetime.now()

        if time_left.total_seconds() > 0:
            days_left = time_left.days
            hours_left = time_left.seconds // 3600
            message = (f"â³ You are currently on a free trial.\n"
                       f"It will expire in approximately *{days_left} days and {hours_left} hours*.\n\n"
                       f"You can use /pay at any time to get a permanent subscription.")
        else:
            message = "âŒ Your free trial has expired.\nTo continue using the bot, please use the /pay command to subscribe."
            
        await update.message.reply_text(message, parse_mode='Markdown')

    except (ValueError, TypeError):
        await update.message.reply_text("Could not determine your trial status. Please contact an admin.")


# --- MAIN ---
def main():
    # Original initialization first
    initialize_database()
    # âœ… Call new function to add features to DB
    initialize_database_additions()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Conversations (Original)
    payment_proof_conv = ConversationHandler(entry_points=[CallbackQueryHandler(submit_payment_proof_start, pattern='^submit_payment_proof$')], states={AWAIT_PAYMENT_PROOF: [MessageHandler(filters.PHOTO, received_payment_proof)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)
    upload_conv = ConversationHandler(entry_points=[CommandHandler('upload', lambda u,c: command_wrapper(u,c,upload_start,True)), CallbackQueryHandler(lambda u,c: command_wrapper(u,c,upload_start,True), pattern='^start_upload$')], states={AWAIT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_title)], AWAIT_THUMBNAIL: [MessageHandler(filters.PHOTO, received_thumbnail)], AWAIT_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_duration)], AWAIT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_link)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)
    task_proof_conv = ConversationHandler(entry_points=[CommandHandler('submitproof', lambda u,c: command_wrapper(u,c,submit_task_proof_start,True)), CallbackQueryHandler(lambda u,c: command_wrapper(u,c,submit_task_proof_start,True), pattern='^submit_task_proof$')], states={AWAIT_TASK_PROOF: [MessageHandler(filters.VIDEO, received_task_proof)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)
    rejection_conv = ConversationHandler(entry_points=[CallbackQueryHandler(handle_verification_callback, pattern='^verify_reject_.*$')], states={AWAIT_REJECTION_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_rejection_reason)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)
    price_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_set_price_start, pattern='^admin_set_price$')], states={AWAIT_PAYMENT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_received_price)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)
    instructions_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_set_instructions_start, pattern='^admin_set_instructions$')], states={AWAIT_PAYMENT_INSTRUCTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_received_instructions)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)
    photo_conv = ConversationHandler(entry_points=[CallbackQueryHandler(admin_set_photo_start, pattern='^admin_set_photo$')], states={AWAIT_PAYMENT_PHOTO: [MessageHandler(filters.PHOTO, admin_received_photo)]}, fallbacks=[CommandHandler('cancel', cancel_conversation)], per_message=False)

    application.add_handler(payment_proof_conv)
    application.add_handler(upload_conv)
    application.add_handler(task_proof_conv)
    application.add_handler(rejection_conv)
    application.add_handler(price_conv)
    application.add_handler(instructions_conv)
    application.add_handler(photo_conv)

    # âœ… NEW CONVERSATION HANDLERS
    appeal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(appeal_start, pattern=r"^appeal_report_")],
        states={
            AWAIT_APPEAL_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_appeal_reason)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False
    )
    report_conv = ConversationHandler(
        entry_points=[CommandHandler("report", report_start)],
        states={
            AWAIT_REPORT_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_report_user_id)],
            AWAIT_REPORT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_report_reason)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False
    )
    subscription_proof_conv = ConversationHandler(
        entry_points=[CommandHandler('submitpaymentproof', submit_payment_proof_convo_start)],
        states={AWAIT_SUBSCRIPTION_PROOF: [MessageHandler(filters.PHOTO, received_subscription_proof)]},
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False
    )
    set_trial_days_conv = ConversationHandler(
        entry_points=[CommandHandler('settrialdays', admin_set_trial_days_start)],
        states={AWAIT_TRIAL_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_received_trial_days)]},
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False
    )
    application.add_handler(appeal_conv)
    application.add_handler(subscription_proof_conv)
    application.add_handler(set_trial_days_conv)
    application.add_handler(report_conv)

    # Commands (Original)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("approve", admin_approve_payment, filters=filters.Chat(chat_id=ADMIN_IDS)))
    application.add_handler(CommandHandler("approve", approve_user_command))
    application.add_handler(CommandHandler("rules", lambda u,c: command_wrapper(u,c,rules_command)))
    application.add_handler(CommandHandler("menu", lambda u,c: command_wrapper(u,c,menu_command)))
    application.add_handler(CommandHandler("gettask", lambda u,c: command_wrapper(u,c,get_task_command)))
    application.add_handler(CommandHandler("status", lambda u,c: command_wrapper(u,c,my_status_command)))
    application.add_handler(CommandHandler("leaderboard", lambda u,c: command_wrapper(u,c,leaderboard_command)))
    application.add_handler(CommandHandler("remove", lambda u,c: command_wrapper(u,c,remove_video_start)))
    application.add_handler(CommandHandler("close", lambda u,c: command_wrapper(u,c,toggle_participation)))
    application.add_handler(CommandHandler("open", lambda u,c: command_wrapper(u,c,toggle_participation)))
    application.add_handler(CommandHandler("adminpanel", admin_panel_command))
    application.add_handler(CommandHandler("myreports", my_reports_command))
    
    # âœ… NEW COMMAND HANDLERS
    application.add_handler(CommandHandler("pay", pay_command))
    application.add_handler(CommandHandler("subscribe", pay_command)) # Alias for /pay
    application.add_handler(CommandHandler("trialstatus", trial_status_command))
    application.add_handler(CommandHandler("block", admin_block_user))
    application.add_handler(CommandHandler("unblock", admin_unblock_user))
    application.add_handler(CommandHandler("addstrike", admin_add_strike))
    application.add_handler(CommandHandler("removestrike", admin_remove_strike))
    application.add_handler(CommandHandler("pendingproofs", admin_get_pending_proofs))
    application.add_handler(CallbackQueryHandler(admin_user_management_panel, pattern="^admin_user_management$"))
    application.add_handler(CallbackQueryHandler(admin_show_command_instructions, pattern="^instruct_"))
    application.add_handler(CommandHandler("viewreports", admin_view_reports))
    application.add_handler(CommandHandler("viewreport", admin_view_reports)) # Alias
    
    



    # Callbacks (Original)
    application.add_handler(CallbackQueryHandler(lambda u,c: command_wrapper(u,c,get_task_command), pattern='^get_task$'))
    application.add_handler(CallbackQueryHandler(lambda u,c: command_wrapper(u,c,my_status_command), pattern='^my_status$'))
    application.add_handler(CallbackQueryHandler(lambda u,c: command_wrapper(u,c,remove_video_start), pattern='^remove_video_start$'))
    application.add_handler(CallbackQueryHandler(remove_video_confirm, pattern='^remove_confirm_'))
    application.add_handler(CallbackQueryHandler(lambda u,c: u.callback_query.edit_message_text("Cancelled."), pattern='^remove_cancel$'))
    application.add_handler(CallbackQueryHandler(handle_verification_callback, pattern="^verify_accept_"))
    application.add_handler(CallbackQueryHandler(rate_video_callback, pattern="^rate_"))
    application.add_handler(CallbackQueryHandler(admin_panel_command, pattern="^admin_main_panel$"))
    application.add_handler(CallbackQueryHandler(admin_payment_settings_panel, pattern="^admin_payment_settings$"))
    application.add_handler(CallbackQueryHandler(admin_feature_settings_panel, pattern="^admin_feature_settings$"))
    application.add_handler(CallbackQueryHandler(admin_toggle_setting, pattern=r"^admin_toggle_(payment|reciprocal|quality|credits|tx)$"))
    application.add_handler(CallbackQueryHandler(admin_approve_info, pattern="^admin_approve_info$"))
    application.add_handler(CallbackQueryHandler(admin_remove_photo, pattern="^admin_remove_photo$"))

    # âœ… NEW CALLBACK HANDLER
    application.add_handler(CallbackQueryHandler(handle_subscription_approval, pattern=r"^sub_(approve|reject)_"))

    logger.info("Bot Final Version with new features is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()