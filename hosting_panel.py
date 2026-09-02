import asyncio
import os
import sys
import logging
import subprocess
import psutil
import sqlite3
import hashlib
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import aiohttp
from dotenv import load_dotenv
import re
import shutil

try:
    import git
    from git.exc import GitCommandError
except ImportError:
    git = None
    GitCommandError = Exception

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _persist_container_logs(user_folder: Path, file_name: str, container_id: str):
    """Pull current logs out of a Docker container and write them to the
    on-disk .log file the UI reads from. Call this BEFORE removing/stopping
    a container, since logs are lost once the container is gone."""
    if docker_runner is None:
        return
    try:
        logs = docker_runner.get_logs(container_id)
        log_path = user_folder / f"{Path(file_name).stem}.log"
        log_path.write_text(logs, encoding='utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"Failed to persist logs for {file_name}: {e}")

def safe_extract_zip(zip_ref: zipfile.ZipFile, dest_dir: Path):
    """Extract a ZIP while rejecting any entry that would land outside
    dest_dir (zip-slip protection: '../' traversal, absolute paths, etc)."""
    dest_dir = dest_dir.resolve()
    for member in zip_ref.namelist():
        member_path = (dest_dir / member).resolve()
        if member_path != dest_dir and dest_dir not in member_path.parents:
            raise ValueError(f"Unsafe path in ZIP, refusing to extract: {member}")
    zip_ref.extractall(dest_dir)


# Docker sandbox integration
try:
    from docker_runner import docker_runner, watch_timeout, RUN_TIMEOUT_SECONDS
except Exception as _docker_import_error:
    docker_runner = None
    watch_timeout = None
    RUN_TIMEOUT_SECONDS = 6 * 60 * 60
    logger.warning("Docker runner unavailable: %s", _docker_import_error)
# ────────────────────────────────────────────────────────────────────────────

from error_analyzer import analyze_error, apply_auto_fix

# ─── CONFIG ──────────────────────────────────────────────────────────────
TOKEN = "8860939583:AAH93aBxI9Eek1tT_fjoEfrvzlp9D6Pm1cM"
OWNER_ID = 7265678519
ADMIN_ID = 7265678519
YOUR_USERNAME = "@Xalonexdev03"
UPDATE_CHANNEL = "https://t.me/pdf_making_hub"

# ─── CHANNELS ─────────────────────────────────────────────────────────────
CHANNELS = [
    {"id": -1003999154734, "url": "https://t.me/pdf_making_hub", "name": "DTZO Panel"},
    {"id": -1003999154734, "url": "https://t.me/pdf_making_hub", "name": "Dark Tech Zone"},
    {"id": -1003999154734, "url": "https://t.me/pdf_making_hub", "name": "Dark Tech Zone 2"},
]

BASE_DIR = Path(__file__).parent.absolute()
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'
IROTECH_DIR = BASE_DIR / 'inf'
DATABASE_PATH = IROTECH_DIR / 'bot_data.db'

FREE_USER_LIMIT = 20
SUBSCRIBED_USER_LIMIT = 50
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

UPLOAD_BOTS_DIR.mkdir(exist_ok=True)
IROTECH_DIR.mkdir(exist_ok=True)

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

bot_scripts = {}
user_subscriptions = {}
user_files = {}
user_favorites = {}
banned_users = set()
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
bot_stats = {'total_uploads': 0, 'total_downloads': 0, 'total_runs': 0}

# ─── GITHUB IMPORT SYSTEM ────────────────────────────────────────────────
GITHUB_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?/?$")
awaiting_github_url = set()

def is_valid_github_url(url: str) -> bool:
    return bool(GITHUB_URL_RE.fullmatch(url.strip()))

def clone_github_repo(repo_url: str, user_id: int, bot_name: str):
    """Clone a public GitHub repository into the user's hosting directory."""
    if git is None:
        return False, "GitPython is not installed. Add GitPython to requirements.txt."
    if not is_valid_github_url(repo_url):
        return False, "Invalid GitHub repository URL."

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", bot_name).strip(".-")[:40] or "github-repo"
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    user_folder.mkdir(parents=True, exist_ok=True)
    repos_dir = user_folder / "github_repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    dest = repos_dir / safe_name

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    try:
        git.Repo.clone_from(repo_url, str(dest), depth=1)
        git_dir = dest / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)
        return True, str(dest)
    except GitCommandError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Clone failed: {e}"
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Clone failed: {e}"

def github_repo_stats(repo_path: str):
    root = Path(repo_path)
    counts = {"py": 0, "js": 0, "other": 0}
    total = 0
    for item in root.rglob("*"):
        if not item.is_file() or ".git" in item.parts:
            continue
        total += 1
        ext = item.suffix.lower()
        if ext == ".py":
            counts["py"] += 1
        elif ext == ".js":
            counts["js"] += 1
        else:
            counts["other"] += 1
    return total, counts

# ─── DATABASE FUNCTIONS ──────────────────────────────────────────────────
def migrate_db():
    logger.info("Running database migrations...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        
        c.execute("PRAGMA table_info(user_files)")
        columns = [row[1] for row in c.fetchall()]
        if 'upload_date' not in columns:
            logger.info("Adding upload_date column to user_files table...")
            c.execute('ALTER TABLE user_files ADD COLUMN upload_date TEXT')
            logger.info("upload_date column added successfully.")
        
        c.execute("PRAGMA table_info(active_users)")
        columns = [row[1] for row in c.fetchall()]
        if 'join_date' not in columns:
            logger.info("Adding join_date column to active_users table...")
            c.execute('ALTER TABLE active_users ADD COLUMN join_date TEXT')
            logger.info("join_date column added successfully.")
        if 'last_active' not in columns:
            logger.info("Adding last_active column to active_users table...")
            c.execute('ALTER TABLE active_users ADD COLUMN last_active TEXT')
            logger.info("last_active column added successfully.")

        c.execute("PRAGMA table_info(pending_approvals)")
        columns = [row[1] for row in c.fetchall()]
        if columns and 'notified' not in columns:
            logger.info("Adding notified column to pending_approvals table...")
            c.execute('ALTER TABLE pending_approvals ADD COLUMN notified INTEGER DEFAULT 0')
            logger.info("notified column added successfully.")

        conn.commit()
        conn.close()
        logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.error(f"Database migration error: {e}", exc_info=True)

def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT, upload_date TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY, join_date TEXT, last_active TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, banned_date TEXT, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorites
                     (user_id INTEGER, file_name TEXT, PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_stats
                     (stat_name TEXT PRIMARY KEY, stat_value INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS github_repos
                     (user_id INTEGER, repo_url TEXT, repo_name TEXT, repo_path TEXT, cloned_at TEXT,
                      PRIMARY KEY (user_id, repo_name))''')
        # ── new: per-script env vars (never hardcoded into the script file) ──
        c.execute('''CREATE TABLE IF NOT EXISTS script_env
                     (user_id INTEGER, file_name TEXT, env_json TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        # ── new: scheduled (cron-like) runs ──
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_runs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, file_name TEXT,
                      schedule_type TEXT, hour INTEGER, minute INTEGER,
                      last_run TEXT, next_run TEXT, active INTEGER DEFAULT 1)''')
        # ── new: two-approver system for free-tier script runs ──
        c.execute('''CREATE TABLE IF NOT EXISTS pending_approvals
                     (user_id INTEGER, file_name TEXT, status TEXT DEFAULT 'pending',
                      requested_at TEXT, decided_at TEXT, decided_by TEXT, reason TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS muted_users
                     (user_id INTEGER PRIMARY KEY, mute_until TEXT, reason TEXT)''')

        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        
        for stat in ['total_uploads', 'total_downloads', 'total_runs']:
            c.execute('INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES (?, 0)', (stat,))
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}", exc_info=True)

def load_data():
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                logger.warning(f"Invalid expiry date for user {user_id}")
        
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))
        
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())
        
        c.execute('SELECT user_id, file_name FROM favorites')
        for user_id, file_name in c.fetchall():
            if user_id not in user_favorites:
                user_favorites[user_id] = []
            user_favorites[user_id].append(file_name)
        
        c.execute('SELECT stat_name, stat_value FROM bot_stats')
        for stat_name, stat_value in c.fetchall():
            bot_stats[stat_name] = stat_value
        
        conn.close()
        logger.info(f"Data loaded: {len(active_users)} users, {len(banned_users)} banned, {len(admin_ids)} admins.")
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

init_db()
migrate_db()
load_data()

def get_user_file_limit(user_id):
    if user_id == OWNER_ID: return OWNER_LIMIT
    if user_id in admin_ids: return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def is_free_tier(user_id) -> bool:
    """Admins and active premium subscribers skip the approval gate —
    only free-tier users need a bot/admin approval before their script runs."""
    if user_id in admin_ids:
        return False
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return False
    return True

def get_mute_remaining(user_id):
    """Returns remaining timedelta if the user is currently muted
    (from a failed bot script review), else None."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('SELECT mute_until FROM muted_users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    try:
        mute_until = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    remaining = mute_until - datetime.now()
    return remaining if remaining.total_seconds() > 0 else None

def request_approval(user_id: int, file_name: str):
    """Queues a free-tier upload for review. approval_bot.py (separate
    process) and the admin panel both write decisions to this same row —
    whichever decides first wins."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute(
        'INSERT OR REPLACE INTO pending_approvals (user_id, file_name, status, requested_at) '
        "VALUES (?, ?, 'pending', ?)",
        (user_id, file_name, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

async def notify_admins_new_pending(user_id: int, file_name: str):
    """Pushes a message to every admin the moment a free-tier upload needs
    review, instead of making them poll the Pending Approvals panel."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_script:{user_id}:{file_name}"),
         InlineKeyboardButton(text="🚫 Reject", callback_data=f"reject_script:{user_id}:{file_name}")]
    ])
    text = (
        f"🕵️ <b>New script awaiting approval</b>\n\n"
        f"👤 User: <code>{user_id}</code>\n"
        f"📄 File: <code>{file_name}</code>\n\n"
        f"The approval bot is also scanning this automatically — whichever decides first wins."
    )
    for admin_id in list(admin_ids):
        try:
            await bot.send_message(admin_id, text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Could not notify admin {admin_id} of pending approval: {e}")

async def approval_result_notifier():
    """Background loop: sends the user their approve/reject result via the
    MAIN bot (which they've already started, since they used it to upload)
    instead of approval_bot.py's own bot — avoids 'chat not found' errors
    for users who never separately /start'd the approval bot."""
    while True:
        await asyncio.sleep(10)
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            c = conn.cursor()
            c.execute(
                "SELECT user_id, file_name, status, decided_by, reason FROM pending_approvals "
                "WHERE status IN ('approved', 'rejected') AND (notified IS NULL OR notified = 0)"
            )
            rows = c.fetchall()
            for user_id, file_name, status, decided_by, reason in rows:
                if status == 'approved':
                    text = (
                        f"✅ <code>{file_name}</code> was approved "
                        f"({'automatic review' if decided_by == 'bot' else 'admin review'}) — you can run it now."
                    )
                else:
                    text = (
                        f"🚫 <b>Script blocked:</b> <code>{file_name}</code>\n\n"
                        f"Reason: {reason or 'rejected in review'}\n\n"
                        + ("⏱️ You're muted from running free-tier scripts for 2 hours.\n\n" if decided_by == 'bot' else "")
                        + "Contact an admin if this looks wrong."
                    )
                try:
                    await bot.send_message(user_id, text, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"Could not notify user {user_id} of approval result: {e}")
                c.execute(
                    'UPDATE pending_approvals SET notified = 1 WHERE user_id = ? AND file_name = ?',
                    (user_id, file_name)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"approval_result_notifier error: {e}")

def get_approval_status(user_id: int, file_name: str):
    """Returns 'pending' | 'approved' | 'rejected' | None (never uploaded/requested)."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('SELECT status FROM pending_approvals WHERE user_id = ? AND file_name = ?', (user_id, file_name))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_script_env(user_id: int, file_name: str) -> dict:
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('SELECT env_json FROM script_env WHERE user_id = ? AND file_name = ?', (user_id, file_name))
    row = c.fetchone()
    conn.close()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return {}

def set_script_env(user_id: int, file_name: str, env: dict):
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute(
        'INSERT OR REPLACE INTO script_env (user_id, file_name, env_json) VALUES (?, ?, ?)',
        (user_id, file_name, json.dumps(env))
    )
    conn.commit()
    conn.close()

# Text-input capture states (mirrors the existing awaiting_github_url pattern)
awaiting_env_for = {}   # user_id -> file_name, while we wait for their "KEY=VALUE" lines

def get_main_keyboard(user_id):
    if user_id in admin_ids:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Updates", url=UPDATE_CHANNEL)],
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file"),
             InlineKeyboardButton(text="🐙 GitHub Repo", callback_data="github_import")],
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="⭐ Favorites", callback_data="my_favorites"),
             InlineKeyboardButton(text="🔍 Search Files", callback_data="search_files")],
            [InlineKeyboardButton(text="⚡ Bot Speed", callback_data="bot_speed"),
             InlineKeyboardButton(text="📊 My Stats", callback_data="statistics")],
            [InlineKeyboardButton(text="📈 Resource Dashboard", callback_data="resource_dashboard")],
            [InlineKeyboardButton(text="ℹ️ Help & Info", callback_data="help_info"),
             InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
            [InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel"),
             InlineKeyboardButton(text="💬 Contact", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Updates Channel", url=UPDATE_CHANNEL)],
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file"),
             InlineKeyboardButton(text="🐙 GitHub Repo", callback_data="github_import")],
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
            [InlineKeyboardButton(text="⭐ Favorites", callback_data="my_favorites"),
             InlineKeyboardButton(text="🔍 Search Files", callback_data="search_files")],
            [InlineKeyboardButton(text="⚡ Bot Speed", callback_data="bot_speed"),
             InlineKeyboardButton(text="📊 My Stats", callback_data="statistics")],
            [InlineKeyboardButton(text="📈 Resource Dashboard", callback_data="resource_dashboard")],
            [InlineKeyboardButton(text="💎 Get Premium", callback_data="get_premium"),
             InlineKeyboardButton(text="ℹ️ Help", callback_data="help_info")],
            [InlineKeyboardButton(text="🎯 Features", callback_data="all_features"),
             InlineKeyboardButton(text="💬 Contact Owner", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")]
        ])
    return keyboard

def get_admin_panel_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 User Stats", callback_data="admin_total_users"),
         InlineKeyboardButton(text="📁 Files Stats", callback_data="admin_total_files")],
        [InlineKeyboardButton(text="🚀 Running Scripts", callback_data="admin_running_scripts"),
         InlineKeyboardButton(text="💎 Premium Users", callback_data="admin_premium_users")],
        [InlineKeyboardButton(text="🕵️ Pending Approvals", callback_data="admin_pending_approvals")],
        [InlineKeyboardButton(text="➕ Add Admin", callback_data="admin_add_admin"),
         InlineKeyboardButton(text="➖ Remove Admin", callback_data="admin_remove_admin")],
        [InlineKeyboardButton(text="🚫 Ban User", callback_data="admin_ban_user"),
         InlineKeyboardButton(text="✅ Unban User", callback_data="admin_unban_user")],
        [InlineKeyboardButton(text="📊 Bot Analytics", callback_data="admin_analytics"),
         InlineKeyboardButton(text="⚙️ System Info", callback_data="admin_system_status")],
        [InlineKeyboardButton(text="🔒 Lock/Unlock", callback_data="lock_bot"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton(text="🗑️ Clean Files", callback_data="admin_clean_files"),
         InlineKeyboardButton(text="💾 Backup DB", callback_data="admin_backup_db")],
        [InlineKeyboardButton(text="📝 View Logs", callback_data="admin_view_logs"),
         InlineKeyboardButton(text="🔄 Restart Bot", callback_data="admin_restart_bot")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    return keyboard

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in banned_users:
        await message.answer("🚫 <b>You are banned from using this bot!</b>\n\nContact admin for more info.", parse_mode="HTML")
        return
    
    active_users.add(user_id)
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute('INSERT OR REPLACE INTO active_users (user_id, join_date, last_active) VALUES (?, ?, ?)', 
                  (user_id, now, now))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving active user: {e}")
    
    welcome_text = f"""
╔═══════════════════════╗
    🌟 <b>WELCOME TO FILE HOST BOT</b> 🌟
╚═══════════════════════╝

👋 <b>Hi,</b> {message.from_user.full_name}!

🆔 <b>Your ID:</b> <code>{user_id}</code>
📦 <b>Upload Limit:</b> {get_user_file_limit(user_id)} files
💎 <b>Account:</b> {'Premium ✨' if user_id in user_subscriptions else 'Free 🆓'}

━━━━━━━━━━━━━━━━━━━━
<b>🎯 FREE USER FEATURES:</b>

📤 <b>Upload Files</b> - Upload Python, JS, ZIP files
📁 <b>Manage Files</b> - View, delete, organize
⭐ <b>Add Favorites</b> - Quick access to files
🔍 <b>Search Files</b> - Find files easily
▶️ <b>Run Scripts</b> - Execute Python/JS code
🛑 <b>Stop Scripts</b> - Control running code
📊 <b>View Stats</b> - Your usage statistics
⚡ <b>Speed Test</b> - Check bot response
📥 <b>Download Files</b> - Get your files
💾 <b>File Info</b> - Size, type, date details
📄 <b>View Logs</b> - See script output logs
📋 <b>Copy Logs</b> - Download log files
ℹ️ <b>Help & Support</b> - Get assistance
🎯 <b>Feature List</b> - Explore all features

━━━━━━━━━━━━━━━━━━━━
<b>✨ Start exploring now! ✨</b>
"""
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")

@dp.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    awaiting_github_url.discard(user_id)
    
    welcome_text = f"""
╔═══════════════════════╗
    🏠 <b>MAIN MENU</b> 🏠
╚═══════════════════════╝

👤 <b>User:</b> {callback.from_user.full_name}
🆔 <b>ID:</b> <code>{user_id}</code>
📦 <b>Files:</b> {len(user_files.get(user_id, []))}/{get_user_file_limit(user_id)}

Use buttons below to navigate 👇
"""
    await callback.message.edit_text(welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "upload_file")
async def callback_upload_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if bot_locked and user_id not in admin_ids:
        await callback.answer("🔒 Bot is locked for maintenance!", show_alert=True)
        return
    
    current_files = len(user_files.get(user_id, []))
    limit = get_user_file_limit(user_id)
    
    upload_text = f"""
╔═══════════════════════╗
    📤 <b>UPLOAD FILES</b> 📤
╚═══════════════════════╝

📊 <b>Current Usage:</b> {current_files}/{limit} files

📝 <b>Supported Formats:</b>
🐍 Python (.py)
🟨 JavaScript (.js)
📦 ZIP Archives (.zip)

━━━━━━━━━━━━━━━━━━━━
<b>💡 How to Upload:</b>

1️⃣ Send your file to the bot
2️⃣ Wait for upload confirmation
3️⃣ File will be saved automatically

⚡ <b>Upload limit:</b> {limit} files
🔥 <b>Quick & Easy!</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(upload_text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "check_files")
async def callback_check_files(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    
    if not files:
        text = """
╔═══════════════════════╗
    📁 <b>MY FILES</b> 📁
╚═══════════════════════╝

📭 <b>No files found!</b>

Upload your first file to get started! 🚀
"""
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ])
    else:
        text = f"""
╔═══════════════════════╗
    📁 <b>MY FILES ({len(files)})</b> 📁
╚═══════════════════════╝

"""
        buttons = []
        for i, (file_name, file_type) in enumerate(files, 1):
            icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
            text += f"{i}. {icon} <code>{file_name}</code>\n"
            
            is_favorite = file_name in user_favorites.get(user_id, [])
            star = "⭐" if is_favorite else "☆"
            
            # Check for log file existence
            user_folder = UPLOAD_BOTS_DIR / str(user_id)
            log_file = user_folder / f"{Path(file_name).stem}.log"
            has_log = log_file.exists()
            
            # Row 1: Run + Favorite
            if file_name.lower() == 'requirements.txt':
                row1 = [
                    InlineKeyboardButton(text="📋 requirements.txt (active)", callback_data=f"file_info:{file_name}"),
                    InlineKeyboardButton(text=f"{star}", callback_data=f"toggle_fav:{file_name}")
                ]
            else:
                row1 = [
                    InlineKeyboardButton(text=f"▶️ Run {file_name[:15]}", callback_data=f"run_script:{file_name}"),
                    InlineKeyboardButton(text=f"{star}", callback_data=f"toggle_fav:{file_name}")
                ]
            buttons.append(row1)
            
            # Row 2: Info + Delete + Logs (if exists)
            row2 = [
                InlineKeyboardButton(text=f"ℹ️ Info {file_name[:15]}", callback_data=f"file_info:{file_name}"),
                InlineKeyboardButton(text=f"🗑️ Delete", callback_data=f"delete_file:{file_name}")
            ]
            if has_log:
                row2.append(InlineKeyboardButton(text="📄 Logs", callback_data=f"view_logs:{file_name}"))
            buttons.append(row2)

            # Row 3: Env Vars + Schedule (runnable scripts only)
            if file_name.lower() != 'requirements.txt':
                buttons.append([
                    InlineKeyboardButton(text="🔑 Env Vars", callback_data=f"env_vars:{file_name}"),
                    InlineKeyboardButton(text="⏰ Schedule", callback_data=f"schedule:{file_name}")
                ])
        
        buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
        back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "my_favorites")
async def callback_my_favorites(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    favorites = user_favorites.get(user_id, [])
    
    if not favorites:
        text = """
╔═══════════════════════╗
    ⭐ <b>FAVORITES</b> ⭐
╚═══════════════════════╝

💭 No favorite files yet!

Add files to favorites for quick access! 🚀
"""
        buttons = [[InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]]
    else:
        text = f"""
╔═══════════════════════╗
    ⭐ <b>FAVORITES ({len(favorites)})</b> ⭐
╚═══════════════════════╝

"""
        buttons = []
        for i, file_name in enumerate(favorites, 1):
            text += f"{i}. ⭐ <code>{file_name}</code>\n"
            user_folder = UPLOAD_BOTS_DIR / str(user_id)
            log_file = user_folder / f"{Path(file_name).stem}.log"
            has_log = log_file.exists()
            row = [
                InlineKeyboardButton(text=f"▶️ {file_name[:20]}", callback_data=f"run_script:{file_name}"),
                InlineKeyboardButton(text=f"❌", callback_data=f"toggle_fav:{file_name}")
            ]
            if has_log:
                row.append(InlineKeyboardButton(text="📄", callback_data=f"view_logs:{file_name}"))
            buttons.append(row)
        
        buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")])
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "search_files")
async def callback_search_files(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    files = user_files.get(user_id, [])
    
    text = f"""
╔═══════════════════════╗
    🔍 <b>SEARCH FILES</b> 🔍
╚═══════════════════════╝

📊 <b>Total Files:</b> {len(files)}

<b>File Types:</b>
🐍 Python: {sum(1 for f in files if f[1] == 'py')}
🟨 JavaScript: {sum(1 for f in files if f[1] == 'js')}
📦 ZIP: {sum(1 for f in files if f[1] == 'zip')}

━━━━━━━━━━━━━━━━━━━━
To search, use:
<code>/search filename</code>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 View All Files", callback_data="check_files")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "bot_speed")
async def callback_bot_speed(callback: types.CallbackQuery):
    start_time = datetime.now()
    await callback.answer("⚡ Testing...")
    end_time = datetime.now()
    speed = (end_time - start_time).total_seconds() * 1000
    
    if speed < 100:
        status = "🟢 Excellent"
        emoji = "🚀"
    elif speed < 300:
        status = "🟡 Good"
        emoji = "⚡"
    else:
        status = "🔴 Slow"
        emoji = "🐌"
    
    text = f"""
╔═══════════════════════╗
    ⚡ <b>SPEED TEST</b> ⚡
╚═══════════════════════╝

{emoji} <b>Response Time:</b> {speed:.2f}ms
📊 <b>Status:</b> {status}

🖥️ <b>Server Info:</b>
• CPU: {psutil.cpu_percent()}%
• Memory: {psutil.virtual_memory().percent}%
• Uptime: Online ✅

✨ Bot is running smoothly!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Test Again", callback_data="bot_speed"),
         InlineKeyboardButton(text="🏠 Home", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "statistics")
async def callback_statistics(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    user_file_count = len(user_files.get(user_id, []))
    user_fav_count = len(user_favorites.get(user_id, []))
    limit = get_user_file_limit(user_id)
    is_premium = user_id in user_subscriptions
    
    text = f"""
╔═══════════════════════╗
    📊 <b>YOUR STATISTICS</b> 📊
╚═══════════════════════╝

👤 <b>User:</b> {callback.from_user.full_name}
🆔 <b>ID:</b> <code>{user_id}</code>

━━━━━━━━━━━━━━━━━━━━
📦 <b>FILE STATISTICS:</b>

📁 Total Files: {user_file_count}/{limit}
⭐ Favorites: {user_fav_count}
💎 Account: {'Premium ✨' if is_premium else 'Free 🆓'}
🚀 Running: {sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))}

━━━━━━━━━━━━━━━━━━━━
📈 <b>USAGE:</b>

📤 Uploads: {bot_stats.get('total_uploads', 0)}
📥 Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}

{'✅ Bot Status: Active' if not bot_locked else '🔒 Bot: Maintenance'}
"""
    
    if user_id in admin_ids:
        text += f"\n━━━━━━━━━━━━━━━━━━━━\n👑 <b>ADMIN STATS:</b>\n"
        text += f"👥 Total Users: {len(active_users)}\n"
        text += f"📁 Total Files: {sum(len(files) for files in user_files.values())}\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "help_info")
async def callback_help_info(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    ℹ️ <b>HELP & INFO</b> ℹ️
╚═══════════════════════╝

<b>🎯 HOW TO USE:</b>

1️⃣ <b>Upload Files:</b>
   • Click 'Upload File'
   • Send your .py, .js, or .zip file
   • File will be saved automatically

2️⃣ <b>Run Scripts:</b>
   • Go to 'My Files'
   • Click 'Run' on any file
   • Monitor script execution

3️⃣ <b>Manage Files:</b>
   • View all files in 'My Files'
   • Add to favorites with ⭐
   • Delete unwanted files (will stop running script)

4️⃣ <b>Search:</b>
   • Use /search [filename]
   • Quick file lookup

5️⃣ <b>Logs:</b>
   • Click '📄 Logs' to view script output
   • Click '📋 Copy Logs' to download full log

━━━━━━━━━━━━━━━━━━━━
<b>💡 COMMANDS:</b>

/start - Start the bot
/help - Show this help
/search - Search files
/stats - Your statistics
/premium - Premium info

<b>Need help? Contact owner! 💬</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "all_features")
async def callback_all_features(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    🎯 <b>ALL FEATURES</b> 🎯
╚═══════════════════════╝

<b>✨ FREE USER FEATURES (15+):</b>

1. 📤 Upload Files (Python, JS, ZIP)
2. 📁 View & Manage Files
3. ⭐ Add to Favorites
4. 🔍 Search Files by Name
5. ▶️ Run Python Scripts
6. ▶️ Run JavaScript Scripts
7. 🛑 Stop Running Scripts
8. 📊 View Your Statistics
9. ⚡ Bot Speed Test
10. 📥 Download Your Files
11. 💾 View File Information
12. 📄 View Script Logs
13. 📋 Copy Logs as File
14. ℹ️ Help & Support
15. 🎯 Feature Discovery

━━━━━━━━━━━━━━━━━━━━
<b>💎 PREMIUM FEATURES:</b>

• 50 file upload limit (vs 20)
• Priority support
• Advanced analytics
• Faster processing
• Premium badge

━━━━━━━━━━━━━━━━━━━━
<b>🔥 Upgrade to Premium!</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Get Premium", callback_data="get_premium")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "get_premium")
async def callback_get_premium(callback: types.CallbackQuery):
    text = """
╔═══════════════════════╗
    💎 <b>PREMIUM PLAN</b> 💎
╚═══════════════════════╝

<b>✨ PREMIUM BENEFITS:</b>

📦 50 File Upload Limit
⚡ Priority Processing
🚀 Faster Response Time
📊 Advanced Analytics
💬 Priority Support
⭐ Premium Badge
🎯 Exclusive Features

━━━━━━━━━━━━━━━━━━━━
<b>💰 PRICING:</b>

1 Month: $5
3 Months: $12 (Save 20%)
1 Year: $40 (Save 33%)

━━━━━━━━━━━━━━━━━━━━
<b>Contact owner to upgrade! 💬</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Contact Owner", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def callback_admin_panel(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id not in admin_ids:
        await callback.answer("❌ Admin access required!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    👑 <b>ADMIN PANEL</b> 👑
╚═══════════════════════╝

<b>🎛️ CONTROL CENTER:</b>

Manage users, files, system settings
and monitor bot performance.

<b>📊 17+ Admin Features Available!</b>

Select an option below to continue...
"""
    
    await callback.message.edit_text(text, reply_markup=get_admin_panel_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("toggle_fav:"))
async def callback_toggle_favorite(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    if user_id not in user_favorites:
        user_favorites[user_id] = []
    
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        
        if file_name in user_favorites[user_id]:
            user_favorites[user_id].remove(file_name)
            c.execute('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            await callback.answer("❌ Removed from favorites!", show_alert=True)
        else:
            user_favorites[user_id].append(file_name)
            c.execute('INSERT OR IGNORE INTO favorites (user_id, file_name) VALUES (?, ?)', (user_id, file_name))
            await callback.answer("⭐ Added to favorites!", show_alert=True)
        
        conn.commit()
        conn.close()
        
        await callback_check_files(callback)
        
    except Exception as e:
        logger.error(f"Error toggling favorite: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

@dp.callback_query(F.data.startswith("file_info:"))
async def callback_file_info(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    
    if not file_path.exists():
        await callback.answer("❌ File not found!", show_alert=True)
        return
    
    file_size = file_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)
    file_ext = file_path.suffix
    modified_time = datetime.fromtimestamp(file_path.stat().st_mtime)
    
    is_favorite = file_name in user_favorites.get(user_id, [])
    
    text = f"""
╔═══════════════════════╗
    ℹ️ <b>FILE INFO</b> ℹ️
╚═══════════════════════╝

📄 <b>Name:</b> <code>{file_name}</code>

📦 <b>Type:</b> {file_ext.upper()} File
💾 <b>Size:</b> {file_size_mb:.2f} MB ({file_size} bytes)
📅 <b>Modified:</b> {modified_time.strftime('%Y-%m-%d %H:%M')}
⭐ <b>Favorite:</b> {'Yes ✨' if is_favorite else 'No'}

🔐 <b>MD5:</b> <code>{hashlib.md5(file_path.read_bytes()).hexdigest()[:16]}...</code>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Run", callback_data=f"run_script:{file_name}"),
         InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file:{file_name}")],
        [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
         InlineKeyboardButton(text="🏠 Home", callback_data="back_to_main")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

# ─── NEW: VIEW LOGS ──────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("view_logs:"))
async def callback_view_logs(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    log_file = user_folder / f"{Path(file_name).stem}.log"

    script_key = f"{user_id}_{file_name}"
    live_info = bot_scripts.get(script_key)
    if live_info is not None:
        _persist_container_logs(user_folder, file_name, live_info['container_id'])

    if not log_file.exists():
        await callback.answer("❌ No logs found for this script.", show_alert=True)
        return
    
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        
        if len(lines) > 200:
            lines = lines[-200:]
            log_text = "".join(lines)
            truncated = True
        else:
            log_text = "".join(lines)
            truncated = False
        
        if not log_text.strip():
            log_text = "(Log file is empty)"
        
        # Escape HTML to avoid parsing errors
        safe_text = log_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        safe_text = safe_text.replace('"', '&quot;').replace("'", '&#39;')
        
        header = f"📄 <b>Logs for:</b> <code>{file_name}</code>\n"
        if truncated:
            header += "⚠️ <i>Showing last 200 lines</i>\n"
        header += "━━━━━━━━━━━━━━━━━━━━━━\n"
        
        max_len = 3900
        full_msg = header + safe_text
        if len(full_msg) > max_len:
            parts = [full_msg[i:i+max_len] for i in range(0, len(full_msg), max_len)]
            for idx, part in enumerate(parts):
                if idx == 0:
                    await callback.message.reply(part, parse_mode="HTML")
                else:
                    await callback.message.reply(f"<pre>{part}</pre>", parse_mode="HTML")
        else:
            await callback.message.reply(full_msg, parse_mode="HTML")
        
        # ─── Copy logs button ──────────────────────────────────────────────
        copy_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Copy Logs (as file)", callback_data=f"copy_logs:{file_name}")],
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
             InlineKeyboardButton(text="🏠 Home", callback_data="back_to_main")]
        ])
        await callback.message.reply(
            "📋 Click below to copy the full log file.",
            reply_markup=copy_kb,
            parse_mode="HTML"
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Error viewing logs: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ─── NEW: COPY LOGS ──────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("copy_logs:"))
async def callback_copy_logs(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    log_file = user_folder / f"{Path(file_name).stem}.log"

    script_key = f"{user_id}_{file_name}"
    live_info = bot_scripts.get(script_key)
    if live_info is not None:
        _persist_container_logs(user_folder, file_name, live_info['container_id'])

    if not log_file.exists():
        await callback.answer("❌ Log file not found.", show_alert=True)
        return
    
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        if not content.strip():
            content = "(Empty log)"
        
        buffer = content.encode('utf-8')
        input_file = types.BufferedInputFile(buffer, filename=f"{Path(file_name).stem}_log.txt")
        
        await callback.answer("📤 Sending log file...", show_alert=False)
        await callback.message.reply_document(
            document=input_file,
            caption=f"📄 <b>Log file:</b> <code>{file_name}.log</code>\n"
                    f"👤 User: <code>{user_id}</code>",
            parse_mode="HTML"
        )
        await callback.answer("✅ Log sent!")
        
    except Exception as e:
        logger.error(f"Error copying logs: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ─── GITHUB REPOSITORY HANDLERS ─────────────────────────────────────────
@dp.callback_query(F.data == "github_import")
async def callback_github_import(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id in banned_users:
        await callback.answer("🚫 You are banned!", show_alert=True)
        return
    if bot_locked and user_id not in admin_ids:
        await callback.answer("🔒 Bot is currently locked!", show_alert=True)
        return
    awaiting_github_url.add(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    await callback.message.edit_text(
        "🐙 <b>DEPLOY FROM GITHUB</b>\n\n"
        "Send a public GitHub repository URL.\n\n"
        "Example:\n<code>https://github.com/user/my-bot</code>\n\n"
        "The repository will be cloned into your hosting directory.",
        reply_markup=kb, parse_mode="HTML"
    )
    await callback.answer()

@dp.message(F.text, ~F.text.startswith("/"))
async def handle_github_text(message: types.Message):
    user_id = message.from_user.id

    if user_id in awaiting_env_for:
        file_name = awaiting_env_for.pop(user_id)
        env = {}
        bad_lines = []
        for line in message.text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if '=' not in line:
                bad_lines.append(line)
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                bad_lines.append(line)
                continue
            env[key] = value.strip()
        if bad_lines:
            await message.answer(
                "⚠️ Skipped invalid line(s) (need <code>KEY=VALUE</code>, key must be letters/numbers/underscore):\n"
                + "\n".join(f"<code>{l}</code>" for l in bad_lines[:5]),
                parse_mode="HTML"
            )
        set_script_env(user_id, file_name, env)
        await message.answer(
            f"✅ Saved {len(env)} env var(s) for <code>{file_name}</code>. They'll be injected on the next run.",
            parse_mode="HTML"
        )
        return

    if user_id not in awaiting_github_url:
        return
    awaiting_github_url.discard(user_id)
    repo_url = message.text.strip()
    if not is_valid_github_url(repo_url):
        await message.answer("❌ Invalid GitHub URL. Please send a URL like:\n<code>https://github.com/user/repo</code>", parse_mode="HTML")
        return

    current_repos = 0
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        current_repos = conn.execute("SELECT COUNT(*) FROM github_repos WHERE user_id=?", (user_id,)).fetchone()[0]
        conn.close()
    except Exception:
        current_repos = 0
    if current_repos >= get_user_file_limit(user_id):
        await message.answer("❌ GitHub repository limit reached. Remove an existing repo or upgrade your plan.")
        return

    bot_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")[:40]
    status = await message.answer("⏳ Cloning GitHub repository...")
    ok, result = await asyncio.to_thread(clone_github_repo, repo_url, user_id, bot_name)
    if not ok:
        await status.edit_text(f"❌ {result}")
        return

    repo_path = Path(result)
    total, counts = github_repo_stats(result)
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.execute("INSERT OR REPLACE INTO github_repos (user_id, repo_url, repo_name, repo_path, cloned_at) VALUES (?, ?, ?, ?, ?)",
                     (user_id, repo_url, bot_name, str(repo_path), datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"GitHub DB save failed: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 My Files", callback_data="check_files")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    await status.edit_text(
        f"✅ <b>GITHUB REPOSITORY CLONED</b>\n\n"
        f"📦 <b>Repository:</b> <code>{bot_name}</code>\n"
        f"📄 <b>Total files:</b> {total}\n"
        f"🐍 Python: {counts['py']}\n"
        f"🟨 JavaScript: {counts['js']}\n"
        f"📦 Other: {counts['other']}\n\n"
        f"📍 <code>{repo_path}</code>\n\n"
        f"⚠️ The repository is imported as a project directory; it is not automatically executed.",
        reply_markup=kb, parse_mode="HTML"
    )

# ─── DOCUMENT HANDLER ──────────────────────────────────────────────────
@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in banned_users:
        await message.answer("🚫 You are banned from using this bot!")
        return
    
    if bot_locked and user_id not in admin_ids:
        await message.answer("🔒 Bot is currently locked!")
        return
    
    document = message.document
    # Only take the basename — strips any '../', absolute paths, or other
    # directory components a crafted client could put in file_name.
    raw_name = document.file_name or "file"
    file_name = Path(raw_name).name
    if not file_name or file_name in (".", ".."):
        await message.answer("❌ Invalid file name!")
        return
    file_ext = os.path.splitext(file_name)[1].lower()
    is_requirements = file_name.lower() == 'requirements.txt'

    if file_ext not in ['.py', '.js', '.zip'] and not is_requirements:
        await message.answer("❌ Only .py, .js, .zip, and requirements.txt files are supported!")
        return
    
    current_files = len(user_files.get(user_id, []))
    limit = get_user_file_limit(user_id)
    
    if current_files >= limit:
        await message.answer(f"❌ Upload limit reached! ({current_files}/{limit})\n\n💎 Upgrade to premium for more space!")
        return
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    user_folder.mkdir(exist_ok=True)
    
    file_path = user_folder / file_name
    
    try:
        file_size_kb = document.file_size / 1024
        
        status_msg = await message.answer(
            f"📤 <b>Preparing upload...</b>\n\n"
            f"📄 File: <code>{file_name}</code>\n"
            f"💾 Size: {file_size_kb:.2f} KB\n\n"
            f"▓░░░░░░░░░ 0%",
            parse_mode="HTML"
        )
        
        await asyncio.sleep(0.3)
        await status_msg.edit_text(
            f"📥 <b>Downloading...</b>\n\n"
            f"📄 File: <code>{file_name}</code>\n"
            f"💾 Size: {file_size_kb:.2f} KB\n\n"
            f"▓▓▓░░░░░░░ 30%",
            parse_mode="HTML"
        )
        
        await bot.download(document, destination=file_path)
        
        await status_msg.edit_text(
            f"💾 <b>Saving to database...</b>\n\n"
            f"📄 File: <code>{file_name}</code>\n"
            f"💾 Size: {file_size_kb:.2f} KB\n\n"
            f"▓▓▓▓▓▓▓░░░ 70%",
            parse_mode="HTML"
        )
        
        if user_id not in user_files:
            user_files[user_id] = []
        
        user_files[user_id].append((file_name, file_ext[1:]))
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
                  (user_id, file_name, file_ext[1:], now))
        c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_uploads',))
        conn.commit()
        conn.close()
        
        bot_stats['total_uploads'] = bot_stats.get('total_uploads', 0) + 1

        needs_approval = is_free_tier(user_id) and file_ext in ('.py', '.js')
        if needs_approval:
            request_approval(user_id, file_name)
            await notify_admins_new_pending(user_id, file_name)

        await status_msg.edit_text(
            f"✅ <b>Finalizing...</b>\n\n"
            f"📄 File: <code>{file_name}</code>\n"
            f"💾 Size: {file_size_kb:.2f} KB\n\n"
            f"▓▓▓▓▓▓▓▓▓▓ 100%",
            parse_mode="HTML"
        )
        
        await asyncio.sleep(0.5)
        
        if file_ext == '.zip':
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Extract ZIP", callback_data=f"extract_zip:{file_name}"),
                 InlineKeyboardButton(text="⭐ Add Favorite", callback_data=f"toggle_fav:{file_name}")],
                [InlineKeyboardButton(text="ℹ️ File Info", callback_data=f"file_info:{file_name}"),
                 InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file:{file_name}")],
                [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
                 InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
            ])
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Run Now", callback_data=f"run_script:{file_name}"),
                 InlineKeyboardButton(text="⭐ Add Favorite", callback_data=f"toggle_fav:{file_name}")],
                [InlineKeyboardButton(text="ℹ️ File Info", callback_data=f"file_info:{file_name}"),
                 InlineKeyboardButton(text="🗑️ Delete", callback_data=f"delete_file:{file_name}")],
                [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
                 InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
            ])
        
        approval_note = (
            "\n🕵️ <b>Pending review:</b> free-tier scripts need a quick automatic or "
            "admin approval before they can run — you'll get a message the moment it clears.\n"
            if needs_approval else ""
        )

        await status_msg.edit_text(
            f"""
╔═══════════════════════╗
    ✅ <b>UPLOAD SUCCESS!</b> ✅
╚═══════════════════════╝

📄 <b>File:</b> <code>{file_name}</code>
📦 <b>Type:</b> {file_ext[1:].upper()}
💾 <b>Size:</b> {document.file_size / 1024:.2f} KB
📊 <b>Usage:</b> {current_files + 1}/{limit}
{approval_note}
🎉 File uploaded successfully!
""",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        await message.answer(f"❌ Upload failed: {str(e)}")

# ─── RUN SCRIPT ──────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("run_script:"))
async def callback_run_script(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    
    if not file_path.exists():
        await callback.answer("❌ File not found!", show_alert=True)
        return
    
    script_key = f"{user_id}_{file_name}"
    
    if script_key in bot_scripts:
        await callback.answer("⚠️ Script is already running!", show_alert=True)
        return
    
    file_ext = file_path.suffix.lower()

    if file_ext not in ('.py', '.js'):
        await callback.answer("❌ Cannot run this file type!", show_alert=True)
        return

    if docker_runner is None or not docker_runner.is_available():
        await callback.answer("❌ Sandbox unavailable — Docker isn't running on this host. Contact admin.", show_alert=True)
        logger.error("Refused to run %s: docker_runner unavailable", script_key)
        return

    remaining_mute = get_mute_remaining(user_id)
    if remaining_mute is not None:
        mins = int(remaining_mute.total_seconds() // 60) + 1
        await callback.answer(f"🔇 You're muted for {mins} more min (flagged script). Contact an admin if this seems wrong.", show_alert=True)
        return

    if is_free_tier(user_id):
        status = get_approval_status(user_id, file_name)
        if status != 'approved':
            if status == 'rejected':
                await callback.answer("🚫 This script was rejected in review and can't be run. Contact an admin.", show_alert=True)
            else:
                await callback.answer("⏳ Still waiting on approval (bot or admin) before this can run.", show_alert=True)
            return

    env_vars = get_script_env(user_id, file_name)

    try:
        container_id, error = docker_runner.run_script(file_path, file_ext, user_folder, script_key, env=env_vars)
        if error:
            await callback.answer(f"❌ {error}", show_alert=True)
            return

        bot_scripts[script_key] = {
            'container_id': container_id,
            'file_name': file_name,
            'script_owner_id': user_id,
            'start_time': datetime.now(),
            'user_folder': str(user_folder),
            'type': file_ext[1:],
        }

        async def _timeout_watcher():
            await asyncio.sleep(RUN_TIMEOUT_SECONDS)
            if script_key not in bot_scripts:
                return  # already stopped/reaped
            if docker_runner is None or not docker_runner.is_running(container_id):
                return
            _persist_container_logs(user_folder, file_name, container_id)
            docker_runner.stop_script(container_id)
            bot_scripts.pop(script_key, None)
            try:
                await bot.send_message(
                    user_id,
                    f"⏱️ Script <code>{file_name}</code> was auto-stopped after hitting the max run time.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Could not notify user {user_id} of timeout: {e}")

        asyncio.create_task(_timeout_watcher())

        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', ('total_runs',))
        conn.commit()
        conn.close()
        bot_stats['total_runs'] = bot_stats.get('total_runs', 0) + 1

        await callback.answer("✅ Script started in sandbox!", show_alert=True)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛑 Stop Script", callback_data=f"stop_script:{script_key}")],
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
             InlineKeyboardButton(text="🏠 Home", callback_data="back_to_main")]
        ])

        await callback.message.edit_reply_markup(reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error running script: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ─── STOP SCRIPT ─────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("stop_script:"))
async def callback_stop_script(callback: types.CallbackQuery):
    script_key = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    script_info = bot_scripts.get(script_key)
    if script_info is None or (script_info['script_owner_id'] != user_id and user_id not in admin_ids):
        await callback.answer("❌ Script not found or already stopped!", show_alert=True)
        return

    try:
        container_id = script_info['container_id']

        _persist_container_logs(Path(script_info['user_folder']), script_info['file_name'], container_id)

        if docker_runner is not None:
            docker_runner.stop_script(container_id)

        del bot_scripts[script_key]
        
        await callback.answer("✅ Script stopped successfully!", show_alert=True)
        
        if callback.from_user.id in admin_ids:
            await callback.message.edit_text("🛑 Script stopped!", parse_mode="HTML")
        else:
            await callback_back_to_main(callback)
        
    except Exception as e:
        logger.error(f"Error stopping script: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ─── AUTO-FIX (missing-dependency case only — never edits user's code) ───
@dp.callback_query(F.data.startswith("autofix:"))
async def callback_autofix(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _, script_key, fix_kind, fix_value = callback.data.split(":", 3)

    expected_owner = script_key.split("_", 1)[0]
    if str(user_id) != expected_owner and user_id not in admin_ids:
        await callback.answer("❌ Not your script!", show_alert=True)
        return

    file_name = script_key.split("_", 1)[1]
    owner_id = int(expected_owner)
    user_folder = UPLOAD_BOTS_DIR / str(owner_id)
    file_path = user_folder / file_name

    if not file_path.exists():
        await callback.answer("❌ File not found!", show_alert=True)
        return

    ok, msg = apply_auto_fix(user_folder, fix_kind, fix_value)
    if not ok:
        await callback.answer(f"❌ {msg}", show_alert=True)
        return

    await callback.answer(f"✅ {msg} Retrying...", show_alert=True)

    if script_key in bot_scripts:
        await callback.message.answer("⚠️ Script is already running — stop it first if you want a clean retry.")
        return

    if docker_runner is None or not docker_runner.is_available():
        await callback.message.answer("❌ Sandbox unavailable right now.")
        return

    remaining_mute = get_mute_remaining(owner_id)
    if remaining_mute is not None:
        await callback.message.answer("🔇 This account is currently muted, can't retry yet.")
        return
    if is_free_tier(owner_id) and get_approval_status(owner_id, file_name) != 'approved':
        await callback.message.answer("⏳ Still waiting on approval before this can run.")
        return

    file_ext = file_path.suffix.lower()
    env_vars = get_script_env(owner_id, file_name)
    container_id, error = docker_runner.run_script(file_path, file_ext, user_folder, script_key, env=env_vars)
    if error:
        await callback.message.answer(f"❌ Retry failed: {error}")
        return

    bot_scripts[script_key] = {
        'container_id': container_id,
        'file_name': file_name,
        'script_owner_id': owner_id,
        'start_time': datetime.now(),
        'user_folder': str(user_folder),
        'type': file_ext[1:],
    }
    await callback.message.answer(f"🔁 Retrying <code>{file_name}</code> after auto-fix...", parse_mode="HTML")

# ─── RESOURCE DASHBOARD (live RAM/CPU per script) ────────────────────────
def _build_resource_dashboard_text(user_id: int) -> str:
    is_admin = user_id in admin_ids
    items = [
        (key, info) for key, info in bot_scripts.items()
        if is_admin or info['script_owner_id'] == user_id
    ]
    if not items:
        return "📈 <b>RESOURCE DASHBOARD</b>\n\n💤 No scripts currently running."

    lines = ["📈 <b>RESOURCE DASHBOARD</b>\n"]
    for key, info in items:
        stats = docker_runner.get_stats(info['container_id']) if docker_runner else None
        owner_tag = f" (user {info['script_owner_id']})" if is_admin else ""
        if stats:
            lines.append(
                f"🐳 <code>{info['file_name']}</code>{owner_tag}\n"
                f"   🧠 CPU: {stats['cpu_percent']}%   "
                f"💾 RAM: {stats['mem_used_mb']}MB / {stats['mem_limit_mb']}MB ({stats['mem_percent']}%)"
            )
        else:
            lines.append(f"🐳 <code>{info['file_name']}</code>{owner_tag}\n   ⏳ stats unavailable")
    return "\n\n".join(lines)

@dp.callback_query(F.data == "resource_dashboard")
async def callback_resource_dashboard(callback: types.CallbackQuery):
    text = _build_resource_dashboard_text(callback.from_user.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="resource_dashboard")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

# ─── ENV VARS PER SCRIPT ──────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("env_vars:"))
async def callback_env_vars(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]

    current = get_script_env(user_id, file_name)
    current_text = "\n".join(f"{k}={v}" for k, v in current.items()) or "(none set)"

    awaiting_env_for[user_id] = file_name

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Clear All", callback_data=f"env_vars_clear:{file_name}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="check_files")]
    ])
    await callback.message.edit_text(
        f"🔑 <b>Env Vars — {file_name}</b>\n\n"
        f"<b>Current:</b>\n<code>{current_text}</code>\n\n"
        f"Send me the variables as one <code>KEY=VALUE</code> per line, e.g.:\n"
        f"<code>API_TOKEN=abc123\nDEBUG=true</code>\n\n"
        f"This replaces the full list. Nothing is written into the script file itself — "
        f"it's injected straight into the sandbox at run time.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("env_vars_clear:"))
async def callback_env_vars_clear(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    set_script_env(user_id, file_name, {})
    awaiting_env_for.pop(user_id, None)
    await callback.answer("✅ Env vars cleared!", show_alert=True)
    await callback_check_files(callback)

# ─── SCHEDULED RUNS ────────────────────────────────────────────────────────
def _compute_next_run(schedule_type: str, hour: int = None, minute: int = None) -> datetime:
    now = datetime.now()
    if schedule_type == 'hourly':
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:  # daily at hour:minute
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
    return nxt

@dp.callback_query(F.data.startswith("schedule:"))
async def callback_schedule(callback: types.CallbackQuery):
    file_name = callback.data.split(":", 1)[1]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 Every Hour", callback_data=f"schedule_set:hourly:{file_name}")],
        [InlineKeyboardButton(text="🕘 Daily @ 09:00", callback_data=f"schedule_set:daily0900:{file_name}"),
         InlineKeyboardButton(text="🕛 Daily @ 00:00", callback_data=f"schedule_set:daily0000:{file_name}")],
        [InlineKeyboardButton(text="🗑️ Remove Schedule", callback_data=f"schedule_remove:{file_name}")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="check_files")]
    ])
    await callback.message.edit_text(
        f"⏰ <b>Schedule — {file_name}</b>\n\nPick how often this script should auto-run:",
        reply_markup=keyboard, parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("schedule_set:"))
async def callback_schedule_set(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _, preset, file_name = callback.data.split(":", 2)

    if preset == 'hourly':
        schedule_type, hour, minute = 'hourly', None, None
    elif preset == 'daily0900':
        schedule_type, hour, minute = 'daily', 9, 0
    elif preset == 'daily0000':
        schedule_type, hour, minute = 'daily', 0, 0
    else:
        await callback.answer("❌ Unknown schedule preset.", show_alert=True)
        return

    next_run = _compute_next_run(schedule_type, hour, minute)

    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM scheduled_runs WHERE user_id = ? AND file_name = ?', (user_id, file_name))
    c.execute(
        'INSERT INTO scheduled_runs (user_id, file_name, schedule_type, hour, minute, next_run, active) '
        'VALUES (?, ?, ?, ?, ?, ?, 1)',
        (user_id, file_name, schedule_type, hour, minute, next_run.isoformat())
    )
    conn.commit()
    conn.close()

    await callback.answer(f"✅ Scheduled! Next run: {next_run.strftime('%Y-%m-%d %H:%M')}", show_alert=True)
    await callback_check_files(callback)

@dp.callback_query(F.data.startswith("schedule_remove:"))
async def callback_schedule_remove(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM scheduled_runs WHERE user_id = ? AND file_name = ?', (user_id, file_name))
    conn.commit()
    conn.close()
    await callback.answer("🗑️ Schedule removed.", show_alert=True)
    await callback_check_files(callback)

async def _start_scheduled_run(user_id: int, file_name: str):
    """Shared with callback_run_script's core logic, minus the callback-query
    plumbing — used by the scheduler background loop."""
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    if not file_path.exists() or docker_runner is None or not docker_runner.is_available():
        return
    script_key = f"{user_id}_{file_name}"
    if script_key in bot_scripts:
        return  # already running, skip this tick

    remaining_mute = get_mute_remaining(user_id)
    if remaining_mute is not None:
        return
    if is_free_tier(user_id) and get_approval_status(user_id, file_name) != 'approved':
        return

    file_ext = file_path.suffix.lower()
    if file_ext not in ('.py', '.js'):
        return

    env_vars = get_script_env(user_id, file_name)
    container_id, error = docker_runner.run_script(file_path, file_ext, user_folder, script_key, env=env_vars)
    if error:
        logger.error(f"Scheduled run failed for {script_key}: {error}")
        return

    bot_scripts[script_key] = {
        'container_id': container_id,
        'file_name': file_name,
        'script_owner_id': user_id,
        'start_time': datetime.now(),
        'user_folder': str(user_folder),
        'type': file_ext[1:],
    }
    try:
        await bot.send_message(user_id, f"⏰ Scheduled run started: <code>{file_name}</code>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Could not notify {user_id} of scheduled run: {e}")

async def scheduler_loop():
    """Checks every minute for due scheduled_runs and kicks them off."""
    while True:
        await asyncio.sleep(60)
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            c = conn.cursor()
            c.execute(
                "SELECT id, user_id, file_name, schedule_type, hour, minute FROM scheduled_runs "
                "WHERE active = 1 AND next_run <= ?",
                (datetime.now().isoformat(),)
            )
            due = c.fetchall()
            for row_id, user_id, file_name, schedule_type, hour, minute in due:
                await _start_scheduled_run(user_id, file_name)
                next_run = _compute_next_run(schedule_type, hour, minute)
                c.execute(
                    'UPDATE scheduled_runs SET last_run = ?, next_run = ? WHERE id = ?',
                    (datetime.now().isoformat(), next_run.isoformat(), row_id)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"scheduler_loop error: {e}")

# ─── EXTRACT ZIP ─────────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("extract_zip:"))
async def callback_extract_zip(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    zip_path = user_folder / file_name
    
    if not zip_path.exists():
        await callback.answer("❌ ZIP file not found!", show_alert=True)
        return
    
    if not zipfile.is_zipfile(zip_path):
        await callback.answer("❌ Invalid ZIP file!", show_alert=True)
        return
    
    try:
        status_text = f"""
╔═══════════════════════╗
    📦 <b>EXTRACTING ZIP</b> 📦
╚═══════════════════════╝

📄 File: <code>{file_name}</code>
⏳ Status: <b>Extracting...</b>

Please wait...
"""
        await callback.message.edit_text(status_text, parse_mode="HTML")
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            safe_extract_zip(zip_ref, user_folder)
            all_files = zip_ref.namelist()
        
        registered_files = []
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        now = datetime.now().isoformat()
        
        for extracted_file in all_files:
            if extracted_file.endswith('/'):
                continue
            
            file_path = Path(extracted_file)
            file_ext = file_path.suffix.lower()
            
            if file_ext in ['.py', '.js']:
                just_name = file_path.name
                
                if user_id not in user_files:
                    user_files[user_id] = []
                
                user_files[user_id].append((just_name, file_ext[1:]))
                
                c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
                          (user_id, just_name, file_ext[1:], now))
                
                registered_files.append(just_name)
        
        if user_id in user_files:
            user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
        
        c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
        c.execute('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, file_name))
        conn.commit()
        conn.close()
        
        if zip_path.exists():
            zip_path.unlink()

        # Same two-approver gate that direct uploads go through — otherwise
        # a free-tier user's extracted scripts would sit un-runnable
        # forever (never queued, so never approved).
        if is_free_tier(user_id):
            for reg_name in registered_files:
                request_approval(user_id, reg_name)
            if registered_files:
                await notify_admins_new_pending(
                    user_id,
                    registered_files[0] if len(registered_files) == 1
                    else f"{len(registered_files)} files from {file_name}"
                )

        registered_text = "\n".join([f"  • <code>{f}</code>" for f in registered_files[:10]])
        if len(registered_files) > 10:
            registered_text += f"\n  ... and {len(registered_files) - 10} more files"
        elif len(registered_files) == 0:
            registered_text = "  <i>No .py or .js files found</i>"
        
        current_count = len(user_files.get(user_id, []))
        limit = get_user_file_limit(user_id)
        
        success_text = f"""
╔═══════════════════════╗
    ✅ <b>EXTRACTION SUCCESS!</b> ✅
╚═══════════════════════╝

📄 <b>ZIP File:</b> <code>{file_name}</code>
📊 <b>Total Extracted:</b> {len(all_files)} files
✅ <b>Registered:</b> {len(registered_files)} files (.py, .js)
🗑️ <b>ZIP Deleted:</b> Automatically

<b>📋 Registered Files:</b>
{registered_text}

📦 <b>Your Files:</b> {current_count}/{limit}

✨ Extraction completed successfully!
"""
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 My Files", callback_data="check_files"),
             InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
        ])
        
        await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer("✅ ZIP extracted & registered!")
        
    except zipfile.BadZipFile:
        await callback.answer("❌ Corrupted ZIP file!", show_alert=True)
    except Exception as e:
        logger.error(f"Error extracting ZIP: {e}")
        await callback.answer(f"❌ Extraction failed: {str(e)}", show_alert=True)

# ─── DELETE FILE – NOW STOPS RUNNING SCRIPT ─────────────────────────────
@dp.callback_query(F.data.startswith("delete_file:"))
async def callback_delete_file(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    file_name = callback.data.split(":", 1)[1]
    
    user_folder = UPLOAD_BOTS_DIR / str(user_id)
    file_path = user_folder / file_name
    
    # ─── Stop running script if exists ──────────────────────────────────
    script_key = f"{user_id}_{file_name}"
    if script_key in bot_scripts:
        try:
            script_info = bot_scripts[script_key]
            container_id = script_info['container_id']
            if docker_runner is not None:
                docker_runner.stop_script(container_id)
            del bot_scripts[script_key]
            logger.info(f"Stopped script {file_name} for user {user_id} due to deletion.")
            await callback.answer("🛑 Stopped running script.", show_alert=True)
        except Exception as e:
            logger.error(f"Error stopping script on delete: {e}")
    
    # ─── Delete the file ──────────────────────────────────────────────────
    try:
        if file_path.exists():
            file_path.unlink()
        
        if user_id in user_files:
            user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
        
        if file_name in user_favorites.get(user_id, []):
            user_favorites[user_id].remove(file_name)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
        c.execute('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, file_name))
        conn.commit()
        conn.close()
        
        await callback.answer("✅ File deleted successfully!", show_alert=True)
        await callback_check_files(callback)
        
    except Exception as e:
        logger.error(f"Error deleting file: {e}")
        await callback.answer(f"❌ Error: {str(e)}", show_alert=True)

# ─── ADMIN: PENDING APPROVALS (2-approver system for free-tier scripts) ──
@dp.callback_query(F.data == "admin_pending_approvals")
async def callback_admin_pending_approvals(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return

    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, file_name, requested_at FROM pending_approvals WHERE status = 'pending' ORDER BY requested_at LIMIT 15")
    rows = c.fetchall()
    conn.close()

    if not rows:
        text = "🕵️ <b>PENDING APPROVALS</b>\n\n✅ Nothing waiting on review."
        buttons = [[InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel")]]
    else:
        text = f"🕵️ <b>PENDING APPROVALS ({len(rows)})</b>\n\nEither you or the bot approving is enough to unblock the user.\n"
        buttons = []
        for user_id, file_name, requested_at in rows:
            text += f"\n👤 <code>{user_id}</code> — <code>{file_name}</code>"
            buttons.append([
                InlineKeyboardButton(text=f"✅ Approve", callback_data=f"approve_script:{user_id}:{file_name}"),
                InlineKeyboardButton(text=f"🚫 Reject", callback_data=f"reject_script:{user_id}:{file_name}")
            ])
        buttons.append([InlineKeyboardButton(text="👨‍💼 Admin Panel", callback_data="admin_panel")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_script:"))
async def callback_approve_script(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    _, user_id_str, file_name = callback.data.split(":", 2)
    user_id = int(user_id_str)

    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE pending_approvals SET status='approved', decided_at=?, decided_by='admin' "
        "WHERE user_id=? AND file_name=? AND status='pending'",
        (datetime.now().isoformat(), user_id, file_name)
    )
    conn.commit()
    conn.close()

    try:
        await bot.send_message(user_id, f"✅ <code>{file_name}</code> was approved by an admin — you can run it now.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Could not notify {user_id} of approval: {e}")

    await callback.answer("✅ Approved!", show_alert=True)
    await callback_admin_pending_approvals(callback)

@dp.callback_query(F.data.startswith("reject_script:"))
async def callback_reject_script(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    _, user_id_str, file_name = callback.data.split(":", 2)
    user_id = int(user_id_str)

    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE pending_approvals SET status='rejected', decided_at=?, decided_by='admin', reason='rejected by admin' "
        "WHERE user_id=? AND file_name=? AND status='pending'",
        (datetime.now().isoformat(), user_id, file_name)
    )
    conn.commit()
    conn.close()

    try:
        await bot.send_message(user_id, f"🚫 <code>{file_name}</code> was rejected by an admin and can't be run.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Could not notify {user_id} of rejection: {e}")

    await callback.answer("🚫 Rejected.", show_alert=True)
    await callback_admin_pending_approvals(callback)

# ─── ADMIN CALLBACKS ──────────────────────────────────────────────────────
# (All admin callbacks remain the same as before)
# I'll keep them as they are – they were already in the file.

@dp.callback_query(F.data == "admin_total_users")
async def callback_admin_total_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    user_list = "\n".join([f"• <code>{uid}</code>" for uid in list(active_users)[:15]])
    text = f"""
╔═══════════════════════╗
    👥 <b>USER STATISTICS</b> 👥
╚═══════════════════════╝

📊 <b>Total Users:</b> {len(active_users)}
🚫 <b>Banned:</b> {len(banned_users)}
✅ <b>Active:</b> {len(active_users) - len(banned_users)}

<b>📝 Recent Users (15):</b>
{user_list}

{'...' if len(active_users) > 15 else ''}
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_total_files")
async def callback_admin_total_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    total_files = sum(len(files) for files in user_files.values())
    py_files = sum(1 for files in user_files.values() for f in files if f[1] == 'py')
    js_files = sum(1 for files in user_files.values() for f in files if f[1] == 'js')
    zip_files = sum(1 for files in user_files.values() for f in files if f[1] == 'zip')
    
    text = f"""
╔═══════════════════════╗
    📁 <b>FILE STATISTICS</b> 📁
╚═══════════════════════╝

📊 <b>Total Files:</b> {total_files}

<b>📦 By Type:</b>
🐍 Python: {py_files}
🟨 JavaScript: {js_files}
📦 ZIP: {zip_files}

<b>📈 Top Users:</b>
"""
    
    top_users = sorted(user_files.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    for user_id, files in top_users:
        text += f"• User <code>{user_id}</code>: {len(files)} files\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_running_scripts")
async def callback_admin_running_scripts(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    if not bot_scripts:
        text = """
╔═══════════════════════╗
    🚀 <b>RUNNING SCRIPTS</b> 🚀
╚═══════════════════════╝

💤 No scripts running currently
"""
        buttons = []
    else:
        text = f"""
╔═══════════════════════╗
    🚀 <b>RUNNING ({len(bot_scripts)})</b> 🚀
╚═══════════════════════╝

"""
        buttons = []
        for script_key, info in bot_scripts.items():
            runtime = (datetime.now() - info['start_time']).total_seconds()
            text += f"🔸 <code>{info['file_name']}</code>\n"
            text += f"   Container: <code>{info['container_id'][:12]}</code> | User: {info['script_owner_id']}\n"
            text += f"   Runtime: {int(runtime)}s\n\n"
            buttons.append([InlineKeyboardButton(
                text=f"🛑 Stop {info['file_name'][:15]}", 
                callback_data=f"stop_script:{script_key}"
            )])
    
    buttons.append([InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")])
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_premium_users")
async def callback_admin_premium_users(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    premium_users = [(u, data) for u, data in user_subscriptions.items() if data['expiry'] > datetime.now()]
    
    if not premium_users:
        text = """
╔═══════════════════════╗
    💎 <b>PREMIUM USERS</b> 💎
╚═══════════════════════╝

No active premium subscriptions.
"""
    else:
        text = f"""
╔═══════════════════════╗
    💎 <b>PREMIUM ({len(premium_users)})</b> 💎
╚═══════════════════════╝

"""
        for user_id, data in premium_users:
            expiry_date = data['expiry'].strftime('%Y-%m-%d')
            text += f"💎 User <code>{user_id}</code>\n   Expires: {expiry_date}\n\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add Premium", callback_data="add_premium")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_analytics")
async def callback_admin_analytics(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    📊 <b>BOT ANALYTICS</b> 📊
╚═══════════════════════╝

<b>📈 GLOBAL STATS:</b>

📤 Total Uploads: {bot_stats.get('total_uploads', 0)}
📥 Total Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}
👥 Total Users: {len(active_users)}
📁 Total Files: {sum(len(files) for files in user_files.values())}
🚀 Running Now: {len(bot_scripts)}
⭐ Total Favorites: {sum(len(favs) for favs in user_favorites.values())}

<b>💎 PREMIUM:</b>
Active: {len([u for u in user_subscriptions if user_subscriptions[u]['expiry'] > datetime.now()])}
Expired: {len([u for u in user_subscriptions if user_subscriptions[u]['expiry'] <= datetime.now()])}

<b>🛡️ SECURITY:</b>
Banned Users: {len(banned_users)}
Admins: {len(admin_ids)}
Bot Status: {'🔒 Locked' if bot_locked else '✅ Active'}
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_system_status")
async def callback_admin_system_status(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    text = f"""
╔═══════════════════════╗
    ⚙️ <b>SYSTEM STATUS</b> ⚙️
╚═══════════════════════╝

<b>💻 CPU:</b>
Usage: {cpu}%
{'🟢 Normal' if cpu < 70 else '🟡 High' if cpu < 90 else '🔴 Critical'}

<b>🧠 MEMORY:</b>
Used: {memory.percent}%
Free: {memory.available / (1024**3):.1f} GB
Total: {memory.total / (1024**3):.1f} GB

<b>💾 DISK:</b>
Used: {disk.percent}%
Free: {disk.free / (1024**3):.1f} GB
Total: {disk.total / (1024**3):.1f} GB

<b>🤖 BOT STATUS:</b>
Status: {'🔒 Locked' if bot_locked else '✅ Running'}
Scripts: {len(bot_scripts)} active
Uptime: ✅ Online
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_system_status")],
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_add_admin")
async def callback_admin_add_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    ➕ <b>ADD ADMIN</b> ➕
╚═══════════════════════╝

To add a new admin, use:
<code>/addadmin USER_ID</code>

<b>Example:</b>
<code>/addadmin 123456789</code>

The user will get full admin privileges!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_remove_admin")
async def callback_admin_remove_admin(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    ➖ <b>REMOVE ADMIN</b> ➖
╚═══════════════════════╝

<b>Current Admins ({len(admin_ids)}):</b>

"""
    
    for admin_id in admin_ids:
        text += f"👑 <code>{admin_id}</code>\n"
    
    text += "\n<b>To remove:</b>\n<code>/removeadmin USER_ID</code>"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_ban_user")
async def callback_admin_ban_user(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    🚫 <b>BAN USER</b> 🚫
╚═══════════════════════╝

<b>Currently Banned:</b> {len(banned_users)} users

To ban a user, use:
<code>/ban USER_ID REASON</code>

<b>Example:</b>
<code>/ban 123456789 Spam</code>

Banned users cannot use the bot!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_unban_user")
async def callback_admin_unban_user(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    ✅ <b>UNBAN USER</b> ✅
╚═══════════════════════╝

<b>Banned Users:</b> {len(banned_users)}

"""
    
    if banned_users:
        text += "<b>List:</b>\n"
        for ban_id in list(banned_users)[:10]:
            text += f"🚫 <code>{ban_id}</code>\n"
    
    text += "\n<b>To unban:</b>\n<code>/unban USER_ID</code>"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "lock_bot")
async def callback_lock_bot(callback: types.CallbackQuery):
    global bot_locked
    
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    bot_locked = not bot_locked
    status = "🔒 LOCKED" if bot_locked else "🔓 UNLOCKED"
    
    await callback.answer(f"Bot is now {status}!", show_alert=True)
    await callback_admin_panel(callback)

@dp.callback_query(F.data == "broadcast")
async def callback_broadcast(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = f"""
╔═══════════════════════╗
    📢 <b>BROADCAST</b> 📢
╚═══════════════════════╝

Send a message to all users!

<b>Total Recipients:</b> {len(active_users)}

<b>Command:</b>
<code>/broadcast Your message here</code>

⚠️ Use this feature responsibly!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "add_premium")
async def callback_add_premium(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    💎 <b>ADD PREMIUM</b> 💎
╚═══════════════════════╝

Give premium access to users!

<b>Command:</b>
<code>/addpremium USER_ID DAYS</code>

<b>Examples:</b>
<code>/addpremium 123456789 30</code> (30 days)
<code>/addpremium 987654321 7</code> (7 days)

Premium benefits:
• 50 file limit (vs 20)
• Priority support
• Premium badge
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_clean_files")
async def callback_admin_clean_files(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    🗑️ <b>CLEAN FILES</b> 🗑️
╚═══════════════════════╝

Clean old or unused files from the system.

<b>Options:</b>
• Delete files older than 30 days
• Remove files from banned users
• Clean temp/log files

<b>Command:</b>
<code>/clean OPTION</code>

⚠️ This action cannot be undone!
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_backup_db")
async def callback_admin_backup_db(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    try:
        backup_path = IROTECH_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        
        conn = sqlite3.connect(DATABASE_PATH)
        backup_conn = sqlite3.connect(backup_path)
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()
        
        await callback.answer("✅ Database backed up!", show_alert=True)
        
        await callback.message.answer_document(
            FSInputFile(backup_path),
            caption="💾 <b>Database Backup</b>\n\nCreated: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            parse_mode="HTML"
        )
        
        backup_path.unlink()
        
    except Exception as e:
        logger.error(f"Backup error: {e}")
        await callback.answer(f"❌ Backup failed: {str(e)}", show_alert=True)

@dp.callback_query(F.data == "admin_view_logs")
async def callback_admin_view_logs(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await callback.answer("❌ Admin only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    📝 <b>SYSTEM LOGS</b> 📝
╚═══════════════════════╝

View bot logs and activity.

<b>Available Logs:</b>
• Error logs
• User activity
• Script executions
• Admin actions

Logs are stored in the system directory.
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_restart_bot")
async def callback_admin_restart_bot(callback: types.CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("❌ Owner only!", show_alert=True)
        return
    
    text = """
╔═══════════════════════╗
    🔄 <b>RESTART BOT</b> 🔄
╚═══════════════════════╝

⚠️ <b>WARNING:</b>
This will restart the entire bot!

All running scripts will be stopped.
Users may experience brief downtime.

<b>Only use if necessary!</b>

Use <code>/restart</code> to confirm.
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Admin Panel", callback_data="admin_panel")]
    ])
    
    await callback.message.edit_text(text, reply_markup=back_keyboard, parse_mode="HTML")
    await callback.answer()

# ─── ADMIN COMMANDS ──────────────────────────────────────────────────────
@dp.message(Command("addadmin"))
async def cmd_add_admin(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /addadmin USER_ID")
            return
        
        new_admin_id = int(args[1])
        
        if new_admin_id in admin_ids:
            await message.answer(f"✅ User {new_admin_id} is already an admin!")
            return
        
        admin_ids.add(new_admin_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (new_admin_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{new_admin_id}</code> added as admin!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error adding admin: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("removeadmin"))
async def cmd_remove_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Only owner can remove admins!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /removeadmin USER_ID")
            return
        
        remove_admin_id = int(args[1])
        
        if remove_admin_id == OWNER_ID:
            await message.answer("❌ Cannot remove owner!")
            return
        
        if remove_admin_id not in admin_ids:
            await message.answer(f"❌ User {remove_admin_id} is not an admin!")
            return
        
        admin_ids.remove(remove_admin_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM admins WHERE user_id = ?', (remove_admin_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{remove_admin_id}</code> removed from admins!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error removing admin: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("addpremium"))
async def cmd_add_premium(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 3:
            await message.answer("Usage: /addpremium USER_ID DAYS")
            return
        
        user_id = int(args[1])
        days = int(args[2])
        
        if days <= 0:
            await message.answer("❌ Days must be greater than 0!")
            return
        
        expiry = datetime.now() + timedelta(days=days)
        user_subscriptions[user_id] = {'expiry': expiry}
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)',
                  (user_id, expiry.isoformat()))
        conn.commit()
        conn.close()
        
        await message.answer(
            f"✅ <b>Premium Added!</b>\n\n"
            f"User: <code>{user_id}</code>\n"
            f"Duration: {days} days\n"
            f"Expires: {expiry.strftime('%Y-%m-%d %H:%M')}",
            parse_mode="HTML"
        )
        
    except ValueError:
        await message.answer("❌ Invalid input!")
    except Exception as e:
        logger.error(f"Error adding premium: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("ban"))
async def cmd_ban_user(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split(maxsplit=2)
        if len(args) < 2:
            await message.answer("Usage: /ban USER_ID [REASON]")
            return
        
        ban_user_id = int(args[1])
        reason = args[2] if len(args) > 2 else "No reason provided"
        
        if ban_user_id in admin_ids:
            await message.answer("❌ Cannot ban an admin!")
            return
        
        banned_users.add(ban_user_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO banned_users (user_id, banned_date, reason) VALUES (?, ?, ?)',
                  (ban_user_id, datetime.now().isoformat(), reason))
        conn.commit()
        conn.close()
        
        await message.answer(f"🚫 User <code>{ban_user_id}</code> has been banned!\n\nReason: {reason}", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error banning user: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("unban"))
async def cmd_unban_user(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        args = message.text.split()
        if len(args) != 2:
            await message.answer("Usage: /unban USER_ID")
            return
        
        unban_user_id = int(args[1])
        
        if unban_user_id not in banned_users:
            await message.answer(f"❌ User {unban_user_id} is not banned!")
            return
        
        banned_users.remove(unban_user_id)
        
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM banned_users WHERE user_id = ?', (unban_user_id,))
        conn.commit()
        conn.close()
        
        await message.answer(f"✅ User <code>{unban_user_id}</code> has been unbanned!", parse_mode="HTML")
        
    except ValueError:
        await message.answer("❌ Invalid USER_ID!")
    except Exception as e:
        logger.error(f"Error unbanning user: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Permission denied!")
        return
    
    try:
        broadcast_text = message.text.replace("/broadcast", "", 1).strip()
        
        if not broadcast_text:
            await message.answer("Usage: /broadcast Your message here")
            return
        
        sent_count = 0
        failed_count = 0
        
        status_msg = await message.answer(f"📢 Broadcasting to {len(active_users)} users...")
        
        for user_id in active_users:
            if user_id in banned_users:
                continue
            
            try:
                await bot.send_message(user_id, f"📢 <b>Announcement:</b>\n\n{broadcast_text}", parse_mode="HTML")
                sent_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Failed to send to {user_id}: {e}")
                failed_count += 1
        
        await status_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"✅ Sent: {sent_count}\n"
            f"❌ Failed: {failed_count}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error broadcasting: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("search"))
async def cmd_search_files(message: types.Message):
    user_id = message.from_user.id
    
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("Usage: /search filename")
            return
        
        search_term = args[1].lower()
        user_file_list = user_files.get(user_id, [])
        
        matches = [f for f in user_file_list if search_term in f[0].lower()]
        
        if not matches:
            await message.answer(f"🔍 No files found matching '<code>{search_term}</code>'", parse_mode="HTML")
            return
        
        text = f"🔍 <b>Search Results ({len(matches)}):</b>\n\n"
        
        for file_name, file_type in matches:
            icon = "🐍" if file_type == "py" else "🟨" if file_type == "js" else "📦"
            text += f"{icon} <code>{file_name}</code>\n"
        
        await message.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        await message.answer(f"❌ Error: {str(e)}")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    text = """
╔═══════════════════════╗
    ℹ️ <b>HELP & INFO</b> ℹ️
╚═══════════════════════╝

<b>🎯 HOW TO USE:</b>

1️⃣ <b>Upload Files:</b>
   • Click 'Upload File'
   • Send your .py, .js, or .zip file
   • File will be saved automatically

2️⃣ <b>Run Scripts:</b>
   • Go to 'My Files'
   • Click 'Run' on any file
   • Monitor script execution

3️⃣ <b>Manage Files:</b>
   • View all files in 'My Files'
   • Add to favorites with ⭐
   • Delete unwanted files (will stop running script)

4️⃣ <b>Search:</b>
   • Use /search [filename]
   • Quick file lookup

5️⃣ <b>Logs:</b>
   • Click '📄 Logs' to view script output
   • Click '📋 Copy Logs' to download full log

━━━━━━━━━━━━━━━━━━━━
<b>💡 COMMANDS:</b>

/start - Start the bot
/help - Show this help
/search - Search files
/stats - Your statistics
/premium - Premium info

<b>Need help? Contact owner! 💬</b>
"""
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Features", callback_data="all_features")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await message.answer(text, reply_markup=back_keyboard, parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    user_file_count = len(user_files.get(user_id, []))
    user_fav_count = len(user_favorites.get(user_id, []))
    is_premium = user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now()
    
    text = f"""
╔═══════════════════════╗
    📊 <b>YOUR STATISTICS</b> 📊
╚═══════════════════════╝

<b>👤 USER INFO:</b>

🆔 User ID: <code>{user_id}</code>
👤 Name: {message.from_user.full_name}
📦 Files Uploaded: {user_file_count}/{get_user_file_limit(user_id)}
⭐ Favorites: {user_fav_count}
💎 Account: {'Premium ✨' if is_premium else 'Free 🆓'}
🚀 Running: {sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))}

━━━━━━━━━━━━━━━━━━━━
📈 <b>USAGE:</b>

📤 Uploads: {bot_stats.get('total_uploads', 0)}
📥 Downloads: {bot_stats.get('total_downloads', 0)}
▶️ Script Runs: {bot_stats.get('total_runs', 0)}

{'✅ Bot Status: Active' if not bot_locked else '🔒 Bot: Maintenance'}
"""
    
    if user_id in admin_ids:
        text += f"\n━━━━━━━━━━━━━━━━━━━━\n👑 <b>ADMIN STATS:</b>\n"
        text += f"👥 Total Users: {len(active_users)}\n"
        text += f"📁 Total Files: {sum(len(files) for files in user_files.values())}\n"
    
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_to_main")]
    ])
    
    await message.answer(text, reply_markup=back_keyboard, parse_mode="HTML")

async def web_server():
    app = web.Application()
    
    async def handle(request):
        return web.Response(text="🚀 Advanced File Host Bot - Powered by Aiogram & Aiohttp!")
    
    app.router.add_get('/', handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 5000)
    await site.start()
    logger.info("🌐 Web server started on port 5000")

async def _maybe_offer_autofix(info: dict, container_id: str):
    """If the script crashed (non-zero exit), run the heuristic error
    analyzer on its logs and DM the owner a diagnosis. Only offers a
    one-tap fix for the mechanical case (missing dependency) — never
    rewrites the user's actual code."""
    exit_code = docker_runner.get_exit_code(container_id) if docker_runner else None
    if not exit_code:  # None or 0 -> ran fine (or we can't tell), nothing to say
        return
    log_path = Path(info['user_folder']) / f"{Path(info['file_name']).stem}.log"
    log_text = log_path.read_text(encoding='utf-8', errors='ignore') if log_path.exists() else ""
    diagnosis = analyze_error(log_text, info.get('type', 'py'))
    if diagnosis is None:
        return

    text = (
        f"🩺 <b>Auto-diagnosis for</b> <code>{info['file_name']}</code>\n\n"
        f"❗ {diagnosis['message']}\n"
        f"💡 {diagnosis['suggestion']}"
    )
    keyboard = None
    if diagnosis.get('auto_fixable'):
        script_key = f"{info['script_owner_id']}_{info['file_name']}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔧 Auto-Fix & Retry",
                callback_data=f"autofix:{script_key}:{diagnosis['fix_kind']}:{diagnosis['fix_value']}"
            )]
        ])
    try:
        await bot.send_message(info['script_owner_id'], text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Could not send auto-fix diagnosis to {info['script_owner_id']}: {e}")

async def script_reaper():
    """Periodically checks running scripts; if a container exited on its own
    (script finished, crashed, or was OOM/CPU-killed by its resource limits),
    persist its logs to disk and clean up bot_scripts so 'Run' works again."""
    while True:
        await asyncio.sleep(15)
        if docker_runner is None or not docker_runner.is_available():
            continue
        for script_key in list(bot_scripts.keys()):
            info = bot_scripts.get(script_key)
            if info is None:
                continue
            container_id = info['container_id']
            if not docker_runner.is_running(container_id):
                try:
                    _persist_container_logs(Path(info['user_folder']), info['file_name'], container_id)
                    await _maybe_offer_autofix(info, container_id)
                    docker_runner.stop_script(container_id)  # ensures removal even if already exited
                except Exception as e:
                    logger.error(f"Reaper error for {script_key}: {e}")
                finally:
                    bot_scripts.pop(script_key, None)
        docker_runner.cleanup_finished()

async def main():
    logger.info("🚀 Starting Advanced File Host Bot...")

    if docker_runner is not None and docker_runner.is_available():
        docker_runner.ensure_images()
    else:
        logger.warning("⚠️ Docker sandbox is NOT available — script execution will be disabled until Docker is reachable.")

    asyncio.create_task(web_server())
    asyncio.create_task(script_reaper())
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(approval_result_notifier())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())