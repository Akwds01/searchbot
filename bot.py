import html
import json
import logging
import os
import random
import re
import time
import urllib.parse
import asyncio
import io
import math
import subprocess
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from dotenv import load_dotenv
import yt_dlp
from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    LinkPreviewOptions,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# 1. Konfigurasi Lingkungan, Logging & Global State
# ---------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ ERROR: BOT_TOKEN tidak ditemukan di file .env!")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

FAV_FILE = "favorites.json"

# Pemetaan resmi nama kategori ke slug URL Pornhub
POPULAR_CATEGORIES = [
    ("🌏 Asian", "asian"),
    ("🏠 Amateur", "amateur"),
    ("🌸 Japanese", "japanese"),
    ("👩 MILF", "milf"),
    ("🎨 Hentai", "hentai"),
    ("👩‍❤️‍👩 Lesbian", "lesbian"),
    ("🥽 VR", "vr_porn"),
    ("🎭 Cosplay", "cosplay"),
    ("🖤 Ebony", "ebony"),
    ("🔥 Popular", "popular"),
]

def load_favorites() -> dict:
    if os.path.exists(FAV_FILE):
        try:
            with open(FAV_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_favorites(data: dict):
    with open(FAV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_favorite(user_id: int, video_data: dict) -> bool:
    favs = load_favorites()
    uid_str = str(user_id)
    if uid_str not in favs:
        favs[uid_str] = []
    if not any(v["viewkey"] == video_data["viewkey"] for v in favs[uid_str]):
        favs[uid_str].append(video_data)
        save_favorites(favs)
        return True
    return False

def remove_favorite(user_id: int, viewkey: str) -> bool:
    favs = load_favorites()
    uid_str = str(user_id)
    if uid_str in favs:
        favs[uid_str] = [v for v in favs[uid_str] if v["viewkey"] != viewkey]
        save_favorites(favs)
        return True
    return False

def get_favorites(user_id: int) -> list:
    return load_favorites().get(str(user_id), [])

VIDEO_CACHE = {}
CANCEL_DOWNLOAD_TASKS = set()

async def fetch_media_bytes(url: str) -> tuple[bytes | None, str]:
    if not url:
        return None, ""
    if url.startswith("//"):
        url = "https:" + url
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.pornhub.com/",
    }
    
    try:
        async with AsyncSession(impersonate="chrome120", timeout=15) as session:
            res = await session.get(url, headers=headers)
            if res.status_code == 200:
                content_type = res.headers.get("content-type", "").lower()
                
                if "text/html" in content_type:
                    return None, ""
                
                if "video/webm" in content_type or ".webm" in url.lower():
                    ext = "webm"
                elif "video/mp4" in content_type or ".mp4" in url.lower():
                    ext = "mp4"
                elif "image/gif" in content_type or ".gif" in url.lower():
                    ext = "gif"
                elif "image/webp" in content_type or ".webp" in url.lower():
                    ext = "webp"
                else:
                    ext = "jpg"
                    
                return res.content, ext
    except Exception as e:
        logging.error(f"Error fetching media bytes: {e}")
    return None, ""

# ---------------------------------------------------------
# 2. Web Scraper Modul (Perbaikan Genre & Filter)
# ---------------------------------------------------------
async def scrape_pornhub(query: str, page: int = 1, order: str = "", limit: int = 5) -> list:
    order_param = f"&o={order}" if order else ""
    
    # Penanganan akurat untuk Genre / Kategori vs Pencarian Biasa
    if query.startswith("cat_"):
        cat_slug = query.replace("cat_", "").strip()
        search_url = f"https://www.pornhub.com/video?c={cat_slug}&page={page}{order_param}"
    else:
        encoded_query = urllib.parse.quote(query)
        search_url = f"https://www.pornhub.com/video/search?search={encoded_query}&page={page}{order_param}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with AsyncSession(impersonate="chrome120", timeout=20) as session:
            response = await session.get(search_url, headers=headers)
            logging.info(f"Scraper Status Code ({query}): {response.status_code}")

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                video_items = soup.select("li.pcVideoListItem, div.phimage, div.wrap, li.videoblock")

                results = []
                for item in video_items:
                    a_tag = item.select_one("a[href*='/view_video.php']")
                    if not a_tag:
                        continue

                    href = a_tag.get("href", "")
                    viewkey_match = re.search(r"viewkey=([a-zA-Z0-9]+)", href)
                    if not viewkey_match:
                        continue

                    viewkey = viewkey_match.group(1)
                    title = a_tag.get("title") or a_tag.get_text(strip=True)
                    if "play all" in title.lower() or "playlist" in href.lower():
                        continue

                    img_tag = item.select_one("img")
                    thumb_img = ""
                    preview_gif = ""
                    if img_tag:
                        thumb_img = img_tag.get("data-mediumthumb") or img_tag.get("data-thumb_url") or img_tag.get("data-src") or img_tag.get("src") or ""
                        preview_gif = img_tag.get("data-mediabook") or thumb_img
                        
                        if thumb_img.startswith("//"):
                            thumb_img = "https:" + thumb_img
                        if preview_gif.startswith("//"):
                            preview_gif = "https:" + preview_gif

                    duration_elem = item.select_one("var.duration, span.duration")
                    views_elem = item.select_one("span.views var, class.views")
                    rating_elem = item.select_one("div.value, class.rating")

                    video_data = {
                        "title": title,
                        "duration": duration_elem.get_text(strip=True) if duration_elem else "-",
                        "views": views_elem.get_text(strip=True) if views_elem else "-",
                        "rating": rating_elem.get_text(strip=True) if rating_elem else "-",
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={viewkey}",
                        "viewkey": viewkey,
                        "thumb": thumb_img,
                        "preview": preview_gif,
                    }

                    if title and not any(v["viewkey"] == viewkey for v in results):
                        results.append(video_data)
                        VIDEO_CACHE[viewkey] = video_data

                    if len(results) >= limit:
                        break

                return results
    except Exception as e:
        logging.error(f"Error Scraper: {e}")
    return []

async def scrape_trending_today(limit: int = 5) -> list:
    url = "https://www.pornhub.com/video?o=mv&t=t"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    try:
        async with AsyncSession(impersonate="chrome120", timeout=20) as session:
            response = await session.get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                video_items = soup.select("li.pcVideoListItem, div.phimage, div.wrap, li.videoblock")

                results = []
                for item in video_items:
                    a_tag = item.select_one("a[href*='/view_video.php']")
                    if not a_tag:
                        continue

                    href = a_tag.get("href", "")
                    viewkey_match = re.search(r"viewkey=([a-zA-Z0-9]+)", href)
                    if not viewkey_match:
                        continue

                    viewkey = viewkey_match.group(1)
                    title = a_tag.get("title") or a_tag.get_text(strip=True)
                    if "play all" in title.lower():
                        continue

                    img_tag = item.select_one("img")
                    thumb_img = ""
                    preview_gif = ""
                    if img_tag:
                        thumb_img = img_tag.get("data-mediumthumb") or img_tag.get("data-thumb_url") or img_tag.get("data-src") or img_tag.get("src") or ""
                        preview_gif = img_tag.get("data-mediabook") or thumb_img
                        
                        if thumb_img.startswith("//"):
                            thumb_img = "https:" + thumb_img
                        if preview_gif.startswith("//"):
                            preview_gif = "https:" + preview_gif

                    duration_elem = item.select_one("var.duration, span.duration")
                    views_elem = item.select_one("span.views var, class.views")
                    rating_elem = item.select_one("div.value, class.rating")

                    video_data = {
                        "title": title,
                        "duration": duration_elem.get_text(strip=True) if duration_elem else "-",
                        "views": views_elem.get_text(strip=True) if views_elem else "-",
                        "rating": rating_elem.get_text(strip=True) if rating_elem else "-",
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={viewkey}",
                        "viewkey": viewkey,
                        "thumb": thumb_img,
                        "preview": preview_gif,
                    }

                    if not any(v["viewkey"] == viewkey for v in results):
                        results.append(video_data)
                        VIDEO_CACHE[viewkey] = video_data

                    if len(results) >= limit:
                        break
                return results
    except Exception as e:
        logging.error(f"Error Trending Scraper: {e}")
    
    return await scrape_pornhub("popular", page=1, limit=limit)

async def scrape_top_actresses(limit: int = 6) -> list:
    url = "https://www.pornhub.com/pornstars"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    try:
        async with AsyncSession(impersonate="chrome120", timeout=20) as session:
            response = await session.get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                star_items = soup.select("li.pornstarImage, ul#pornstarsPopularMaster li, div.pornstarBlock")

                results = []
                for item in star_items:
                    a_tag = item.select_one("a[href*='/pornstar/'], a[href*='/model/'], a.title")
                    if not a_tag:
                        continue

                    name_elem = item.select_one("span.title, class.pornstarName, a.title, .name")
                    name = name_elem.get_text(strip=True) if name_elem else a_tag.get_text(strip=True)
                    videos_count_elem = item.select_one("span.videosNumber, class.videos, .rankData")
                    videos_count = videos_count_elem.get_text(strip=True) if videos_count_elem else "Populer"

                    if name and not any(s['name'] == name for s in results):
                        results.append({
                            "name": name,
                            "videos": videos_count,
                        })

                    if len(results) >= limit:
                        break
                return results
    except Exception as e:
        logging.error(f"Error Actress Scraper: {e}")
    return []

async def scrape_related_videos(viewkey: str, limit: int = 5) -> list:
    url = f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    try:
        async with AsyncSession(impersonate="chrome120", timeout=20) as session:
            response = await session.get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                video_items = soup.select("ul#relatedVideosVideos li, li.pcVideoListItem, div.phimage")

                results = []
                for item in video_items:
                    a_tag = item.select_one("a[href*='/view_video.php']")
                    if not a_tag:
                        continue

                    href = a_tag.get("href", "")
                    viewkey_match = re.search(r"viewkey=([a-zA-Z0-9]+)", href)
                    if not viewkey_match:
                        continue

                    vk = viewkey_match.group(1)
                    if vk == viewkey:
                        continue

                    title = a_tag.get("title") or a_tag.get_text(strip=True)
                    if "play all" in title.lower():
                        continue

                    img_tag = item.select_one("img")
                    thumb_img = ""
                    preview_gif = ""
                    if img_tag:
                        thumb_img = img_tag.get("data-mediumthumb") or img_tag.get("data-thumb_url") or img_tag.get("data-src") or img_tag.get("src") or ""
                        preview_gif = img_tag.get("data-mediabook") or thumb_img
                        
                        if thumb_img.startswith("//"):
                            thumb_img = "https:" + thumb_img
                        if preview_gif.startswith("//"):
                            preview_gif = "https:" + preview_gif

                    duration_elem = item.select_one("var.duration, span.duration")
                    views_elem = item.select_one("span.views var, class.views")
                    rating_elem = item.select_one("div.value, class.rating")

                    video_data = {
                        "title": title,
                        "duration": duration_elem.get_text(strip=True) if duration_elem else "-",
                        "views": views_elem.get_text(strip=True) if views_elem else "-",
                        "rating": rating_elem.get_text(strip=True) if rating_elem else "-",
                        "url": f"https://www.pornhub.com/view_video.php?viewkey={vk}",
                        "viewkey": vk,
                        "thumb": thumb_img,
                        "preview": preview_gif,
                    }

                    if title and not any(v["viewkey"] == vk for v in results):
                        results.append(video_data)
                        VIDEO_CACHE[vk] = video_data

                    if len(results) >= limit:
                        break
                return results
    except Exception as e:
        logging.error(f"Error Related Scraper: {e}")
    return []

# ---------------------------------------------------------
# 3. Helper Downloader, Safe FFmpeg Splitting & Progress Bar
# ---------------------------------------------------------
def split_video_file(input_file: str, max_size_mb: int = 45) -> list[str]:
    """Memotong video menggunakan FFmpeg secara aman jika melebihi max_size_mb."""
    if not os.path.exists(input_file):
        return []

    try:
        file_size_mb = os.path.getsize(input_file) / (1024 * 1024)
        if file_size_mb <= max_size_mb:
            return [input_file]

        parts = math.ceil(file_size_mb / max_size_mb)
        
        cmd_duration = [
            "ffprobe", "-v", "error", 
            "-show_entries", "format=duration", 
            "-of", "default=noprintwrappers=1:nokey=1", 
            input_file
        ]
        duration_out = subprocess.check_output(cmd_duration, stderr=subprocess.STDOUT).decode().strip()
        total_duration = float(duration_out)

        part_duration = total_duration / parts
        output_files = []
        base_name, ext = os.path.splitext(input_file)

        for i in range(parts):
            start_time = i * part_duration
            out_part = f"{base_name}_part{i+1}{ext}"
            cmd_split = [
                "ffmpeg", "-y", 
                "-ss", str(start_time), 
                "-i", input_file, 
                "-t", str(part_duration), 
                "-c", "copy", 
                out_part
            ]
            subprocess.run(cmd_split, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_part):
                output_files.append(out_part)

        return output_files if output_files else [input_file]
    except Exception as e:
        logging.error(f"Gagal memotong video via FFmpeg: {e}")
        return [input_file]

def download_video_file(url: str, output_filename: str, quality: str, task_id: str, loop, status_msg) -> tuple[bool, str]:
    last_update = [0]

    def format_time(seconds: float) -> str:
        if not seconds or math.isinf(seconds) or seconds < 0:
            return "--:--"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def progress_hook(d):
        if task_id in CANCEL_DOWNLOAD_TASKS:
            raise Exception("DOWNLOAD_CANCELLED_BY_USER")

        if d['status'] == 'downloading':
            now = time.time()
            if now - last_update[0] >= 2.5:
                last_update[0] = now
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                downloaded = d.get('downloaded_bytes', 0)
                speed = d.get('speed', 0) or 0
                eta = d.get('eta', None)

                downloaded_mb = downloaded / (1024 * 1024)
                
                cancel_markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Batalkan Download", callback_data=f"cancel_dl:{task_id}")]
                ])

                if total > 0:
                    total_mb = total / (1024 * 1024)
                    percent = (downloaded / total) * 100
                    bar_len = 10
                    filled = int(bar_len * downloaded // total)
                    bar = '█' * filled + '░' * (bar_len - filled)
                    
                    speed_mb = speed / (1024 * 1024) if speed else 0
                    eta_str = format_time(eta) if eta is not None else "--:--"
                    
                    msg_text = (
                        f"⏳ <b>Mengunduh ({quality}p):</b>\n"
                        f"<code>[{bar}] {percent:.1f}%</code>\n\n"
                        f"📦 <b>Ukuran:</b> {downloaded_mb:.1f} MB / {total_mb:.1f} MB\n"
                        f"🚀 <b>Kecepatan:</b> {speed_mb:.2f} MB/s\n"
                        f"⏱ <b>Perkiraan Selesai (ETA):</b> {eta_str}"
                    )
                else:
                    speed_mb = speed / (1024 * 1024) if speed else 0
                    msg_text = (
                        f"⏳ <b>Mengunduh ({quality}p):</b>\n"
                        f"📦 <b>Terunduh:</b> {downloaded_mb:.2f} MB\n"
                        f"🚀 <b>Kecepatan:</b> {speed_mb:.2f} MB/s"
                    )
                
                try:
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(msg_text, parse_mode="HTML", reply_markup=cancel_markup), 
                        loop
                    )
                except Exception:
                    pass

    format_opt = "bestaudio/best" if quality == "mp3" else f"best[height<={quality}]/best"
    ydl_opts = {
        "format": format_opt,
        "outtmpl": output_filename,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "progress_hooks": [progress_hook]
    }
    
    if quality == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    direct_url = ""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            direct_url = info.get("url", "")
        return os.path.exists(output_filename), direct_url
    except Exception as e:
        if "DOWNLOAD_CANCELLED_BY_USER" in str(e):
            logging.info("Pengunduhan dibatalkan oleh user.")
            return False, "CANCELLED"
            
        logging.error(f"Error Downloader: {e}")
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "nocheckcertificate": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                direct_url = info.get("url", "")
        except Exception:
            pass
        return False, direct_url

# ---------------------------------------------------------
# 4. Interface Keyboard & Teks Modul
# ---------------------------------------------------------
def build_main_dashboard_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔥 Trending Hari Ini", callback_data="dash:trending"),
            InlineKeyboardButton("⭐ Top Aktres", callback_data="dash:actresses"),
        ],
        [
            InlineKeyboardButton("📂 Kategori / Genre", callback_data="show_cat"),
            InlineKeyboardButton("🎲 Video Acak", callback_data="dash:random"),
        ],
        [
            InlineKeyboardButton("📁 Favorit Saya", callback_data="dash:favorites")
        ]
    ])

def build_search_response(videos: list, query: str, page: int, order: str = ""):
    """Menampilkan 5 video sekaligus dalam daftar ringkas."""
    display_query = query.replace("cat_", "Kategori: ").capitalize()
    safe_query = html.escape(display_query)
    order_label = {"tr": "Top Rated", "mv": "Most Viewed", "mr": "Terbaru"}.get(order, "Standar")
    
    text = f"🎬 <b>HASIL PENCARIAN</b>\n"
    text += f"🔍 Kata Kunci: <b>\"{safe_query}\"</b>\n"
    text += f"📄 Halaman: <b>{page}</b> | 🔀 Urutan: <b>{order_label}</b>\n"
    text += "─────────────────────────\n\n"

    buttons = []
    
    # Tombol Sorting Filter Resmi (mv = Most Viewed, tr = Top Rated, mr = Most Recent)
    order_btns = [
        InlineKeyboardButton("👁 Most Viewed", callback_data=f"sort:mv:{query[:20]}"),
        InlineKeyboardButton("🌟 Top Rated", callback_data=f"sort:tr:{query[:20]}"),
        InlineKeyboardButton("🆕 Terbaru", callback_data=f"sort:mr:{query[:20]}"),
    ]
    buttons.append(order_btns)

    for i, v in enumerate(videos, start=1):
        safe_title = html.escape(v["title"])
        text += (
            f"<b>{i}. {safe_title}</b>\n"
            f"⏱ Durasi: <code>{v['duration']}</code> | 👁 Views: <code>{v['views']}</code> | ⭐ <code>{v['rating']}</code>\n\n"
        )
        buttons.append([
            InlineKeyboardButton(f"🎴 Kartu Detail #{i}", callback_data=f"card:{v['viewkey']}:{i - 1}:{page}:{order}:{query[:20]}"),
            InlineKeyboardButton(f"⚙️ Download #{i}", callback_data=f"opt:{v['viewkey']}")
        ])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(f"◀️ Hal {page - 1}", callback_data=f"page:{page - 1}:{order}:{query[:20]}"))
    
    nav_row.append(InlineKeyboardButton(f"Hal {page + 1} ▶️", callback_data=f"page:{page + 1}:{order}:{query[:20]}"))
    buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="dash:main")])
    return text, InlineKeyboardMarkup(buttons)

def build_card_view(video: dict, index_in_page: int, total_in_page: int, page: int, order: str, query: str):
    """Menampilkan 1 video dalam mode kartu detail."""
    safe_title = html.escape(video["title"])
    global_index = (page - 1) * 5 + index_in_page + 1

    text = f"🎴 <b>KARTU DETAIL VIDEO #{global_index}</b>\n\n"
    text += f"📌 <b>{safe_title}</b>\n"
    text += f"⏱ Durasi  : <code>{video['duration']}</code>\n"
    text += f"👁 Views   : <code>{video['views']}</code>\n"
    text += f"⭐ Rating  : <code>{video['rating']}</code>\n"
    text += f"🔗 <a href=\"{video['url']}\">Tonton di Situs Web</a>\n"

    buttons = []

    buttons.append([
        InlineKeyboardButton("📱 360p", callback_data=f"dl:{video['viewkey']}:360"),
        InlineKeyboardButton("🎬 480p", callback_data=f"dl:{video['viewkey']}:480"),
    ])
    buttons.append([
        InlineKeyboardButton("🖥 720p", callback_data=f"dl:{video['viewkey']}:720"),
        InlineKeyboardButton("🎵 MP3", callback_data=f"dl:{video['viewkey']}:mp3"),
    ])

    buttons.append([
        InlineKeyboardButton("🎞 Preview GIF", callback_data=f"prev:{video['viewkey']}"),
        InlineKeyboardButton("🔗 Video Serupa", callback_data=f"rel:{video['viewkey']}"),
        InlineKeyboardButton("⭐ Simpan Favorit", callback_data=f"fav_add:{video['viewkey']}"),
    ])

    card_nav = []
    if index_in_page > 0:
        card_nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"card_nav:{index_in_page - 1}:{page}:{order}:{query[:20]}"))
    elif page > 1:
        card_nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"card_nav:4:{page - 1}:{order}:{query[:20]}"))

    card_nav.append(InlineKeyboardButton(f"📌 Kartu #{global_index}", callback_data="ignore"))

    if index_in_page < total_in_page - 1:
        card_nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"card_nav:{index_in_page + 1}:{page}:{order}:{query[:20]}"))
    else:
        card_nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"card_nav:0:{page + 1}:{order}:{query[:20]}"))

    buttons.append(card_nav)
    buttons.append([InlineKeyboardButton("📋 Kembali ke Daftar 5 Video", callback_data=f"page:{page}:{order}:{query[:20]}")])
    
    return text, InlineKeyboardMarkup(buttons)

def build_category_keyboard():
    buttons = []
    row = []
    for label, code in POPULAR_CATEGORIES:
        row.append(InlineKeyboardButton(label, callback_data=f"cat:{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="dash:main")])
    return InlineKeyboardMarkup(buttons)

# ---------------------------------------------------------
# 5. Handlers Perintah Utama
# ---------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Selamat Datang di Bot Downloader & Scraper All-In-One!</b>\n\n"
        "<b>Cara Penggunaan:</b>\n"
        "• Ketik langsung kata kunci pencarian di chat.\n"
        "• Atau gunakan tombol navigasi cepat di bawah ini untuk menjelajahi konten populer, genre, dan favorit."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=build_main_dashboard_keyboard())

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.startswith("!phsearch"):
        query = text.replace("!phsearch", "").strip()
    elif text.startswith("/phsearch") or text.startswith("/search"):
        query = text.split(maxsplit=1)[1] if len(text.split()) > 1 else ""
    else:
        query = text

    if not query:
        await update.message.reply_text("Format salah!\nGunakan: <code>!phsearch &lt;kata_kunci&gt;</code>", parse_mode="HTML")
        return

    safe_query = html.escape(query)
    status_msg = await update.message.reply_text(f"🔎 Mencari <b>\"{safe_query}\"</b>...", parse_mode="HTML")

    videos = await scrape_pornhub(query, page=1, limit=5)

    if not videos:
        await status_msg.edit_text("❌ Hasil tidak ditemukan.")
        return

    text_reply, reply_markup = build_search_response(videos, query, page=1)
    await status_msg.edit_text(
        text_reply,
        parse_mode="HTML",
        reply_markup=reply_markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

# ---------------------------------------------------------
# 6. Handler Tombol Interaktif (Callback Queries)
# ---------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    if data == "ignore":
        await query.answer()
        return

    async def safe_render_text(text_reply, reply_markup):
        try:
            await query.edit_message_text(
                text_reply,
                parse_mode="HTML",
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.message.reply_text(
                text_reply,
                parse_mode="HTML",
                reply_markup=reply_markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True)
            )

    # --- NAVIGASI DASHBOARD UTAMA ---
    if data == "dash:main":
        await query.answer()
        text = "🏠 <b>Menu Utama Bot</b>\nPilih opsi berikut:"
        await safe_render_text(text, build_main_dashboard_keyboard())

    elif data == "dash:trending":
        await query.answer()
        status_msg = await query.message.reply_text("🔥 Mengambil **Trending Hari Ini**...", parse_mode="Markdown")
        videos = await scrape_trending_today(limit=5)
        if not videos:
            await status_msg.edit_text("❌ Gagal mengambil data trending.")
            return

        text_reply, reply_markup = build_search_response(videos, "Trending Hari Ini", page=1)
        await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=reply_markup, link_preview_options=LinkPreviewOptions(is_disabled=True))

    elif data == "dash:actresses":
        await query.answer()
        status_msg = await query.message.reply_text("⭐ Mengambil daftar **Top Aktres**...", parse_mode="Markdown")
        actresses = await scrape_top_actresses(limit=6)
        if not actresses:
            await status_msg.edit_text("❌ Gagal mengambil daftar aktres.")
            return

        text_reply = "⭐ <b>TOP AKTRES / PORNSTARS POPULER</b>\nPilih nama aktres di bawah ini untuk melihat koleksi videonya:\n\n"
        buttons = []
        for a in actresses:
            text_reply += f"• <b>{a['name']}</b> ({a['videos']})\n"
            buttons.append([InlineKeyboardButton(f"👩 {a['name']}", callback_data=f"actress:{a['name']}")])
        
        buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="dash:main")])
        await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("actress:"):
        await query.answer()
        actress_name = data.split(":", 1)[1]
        status_msg = await query.message.reply_text(f"🎬 Membuka koleksi video <b>{html.escape(actress_name)}</b>...", parse_mode="HTML")
        videos = await scrape_pornhub(actress_name, page=1, limit=5)
        if not videos:
            await status_msg.edit_text("❌ Tidak ada video ditemukan untuk aktres ini.")
            return

        text_reply, reply_markup = build_search_response(videos, f"Aktres: {actress_name}", page=1)
        await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=reply_markup, link_preview_options=LinkPreviewOptions(is_disabled=True))

    elif data == "dash:random":
        await query.answer()
        status_msg = await query.message.reply_text("🎲 Mengambil video acak...", parse_mode="HTML")
        videos = await scrape_trending_today(limit=10)
        if videos:
            v = random.choice(videos)
            thumb_url = v.get("thumb") or v.get("preview") or ""
            text_reply = (
                f"🎲 <b>VIDEO ACAK PILIHAN:</b>\n\n"
                f"<b>{html.escape(v['title'])}</b>\n"
                f"⏱ Durasi: {v['duration']} | 👁 Views: {v['views']} | ⭐ {v['rating']}\n"
                f"🔗 <a href=\"{v['url']}\">Tonton di Situs</a>"
            )
            buttons = [
                [InlineKeyboardButton("⚙️ Opsi & Download Video Ini", callback_data=f"opt:{v['viewkey']}")],
                [InlineKeyboardButton("🎲 Coba Acak Lagi", callback_data="dash:random")],
                [InlineKeyboardButton("🏠 Menu Utama", callback_data="dash:main")]
            ]
            
            if thumb_url:
                try:
                    await status_msg.delete()
                    await query.message.reply_photo(
                        photo=thumb_url,
                        caption=text_reply,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )
                except Exception:
                    await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
            else:
                await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await status_msg.edit_text("❌ Gagal mengambil video acak.")

    elif data == "dash:favorites":
        await query.answer()
        favs = get_favorites(user_id)
        if not favs:
            await query.message.reply_text("📁 <b>Daftar Favorit Anda masih kosong.</b>\nSimpan video favorit dengan menekan tombol ⭐ pada opsi video.", parse_mode="HTML")
            return

        text_reply = "📁 <b>DAFTAR FAVORIT SAYA:</b>\n\n"
        buttons = []
        for i, v in enumerate(favs, start=1):
            text_reply += f"<b>{i}. {html.escape(v['title'])}</b> ({v['duration']})\n"
            buttons.append([
                InlineKeyboardButton(f"📥 Download #{i}", callback_data=f"opt:{v['viewkey']}"),
                InlineKeyboardButton(f"❌ Hapus #{i}", callback_data=f"fav_del:{v['viewkey']}")
            ])
        buttons.append([InlineKeyboardButton("🏠 Menu Utama", callback_data="dash:main")])
        await query.message.reply_text(text_reply, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

    # --- KATEGORI & SORTING ---
    elif data == "show_cat":
        await query.answer()
        await safe_render_text("📂 <b>Pilih Kategori / Genre:</b>", build_category_keyboard())

    elif data.startswith("cat:"):
        await query.answer()
        cat_code = data.split(":", 1)[1]
        status_msg = await query.message.reply_text(f"📂 Membuka Kategori <b>{cat_code.upper()}</b>...", parse_mode="HTML")
        
        search_query = f"cat_{cat_code}"
        videos = await scrape_pornhub(search_query, page=1, limit=5)
        if not videos:
            await status_msg.edit_text("❌ Tidak dapat mengambil data kategori.")
            return

        text_reply, reply_markup = build_search_response(videos, search_query, page=1)
        await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=reply_markup, link_preview_options=LinkPreviewOptions(is_disabled=True))

    elif data.startswith("sort:"):
        await query.answer()
        _, order, search_query = data.split(":", 2)
        status_msg = await query.message.reply_text("🔄 Mengubah Urutan Filter...", parse_mode="HTML")
        videos = await scrape_pornhub(search_query, page=1, order=order, limit=5)
        if not videos:
            await status_msg.edit_text("❌ Hasil tidak ditemukan.")
            return

        text_reply, reply_markup = build_search_response(videos, search_query, page=1, order=order)
        await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=reply_markup, link_preview_options=LinkPreviewOptions(is_disabled=True))

    elif data.startswith("page:"):
        await query.answer()
        _, page_str, order, search_query = data.split(":", 3)
        page = int(page_str)
        videos = await scrape_pornhub(search_query, page=page, order=order, limit=5)
        if not videos:
            await query.message.reply_text("❌ Tidak ada hasil lagi di halaman ini.")
            return

        text_reply, reply_markup = build_search_response(videos, search_query, page, order)
        await safe_render_text(text_reply, reply_markup)

    # --- HANDLER MODE KARTU DETAIL ---
    elif data.startswith("card:") or data.startswith("card_nav:"):
        await query.answer()
        if data.startswith("card:"):
            _, viewkey, idx_str, page_str, order, search_query = data.split(":", 5)
        else:
            _, idx_str, page_str, order, search_query = data.split(":", 4)

        idx = int(idx_str)
        page = int(page_str)

        videos = await scrape_pornhub(search_query, page=page, order=order, limit=5)
        if videos and 0 <= idx < len(videos):
            target_video = videos[idx]
            text_reply, reply_markup = build_card_view(
                video=target_video,
                index_in_page=idx,
                total_in_page=len(videos),
                page=page,
                order=order,
                query=search_query
            )
            
            thumb_url = target_video.get("thumb") or target_video.get("preview") or ""
            
            if thumb_url:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await query.message.reply_photo(
                    photo=thumb_url,
                    caption=text_reply,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            else:
                await safe_render_text(text_reply, reply_markup)
        else:
            await query.message.reply_text("❌ Data video tidak ditemukan.")

    # --- MENU OPSI VIDEO ---
    elif data.startswith("opt:"):
        await query.answer()
        viewkey = data.split(":", 1)[1]
        buttons = [
            [
                InlineKeyboardButton("📱 360p", callback_data=f"dl:{viewkey}:360"),
                InlineKeyboardButton("🎬 480p", callback_data=f"dl:{viewkey}:480"),
            ],
            [
                InlineKeyboardButton("🖥 720p", callback_data=f"dl:{viewkey}:720"),
                InlineKeyboardButton("🎵 Audio (MP3)", callback_data=f"dl:{viewkey}:mp3"),
            ],
            [
                InlineKeyboardButton("🎞 Preview GIF", callback_data=f"prev:{viewkey}"),
                InlineKeyboardButton("🔗 Video Serupa", callback_data=f"rel:{viewkey}"),
            ],
            [
                InlineKeyboardButton("⭐ Simpan ke Favorit", callback_data=f"fav_add:{viewkey}")
            ]
        ]
        await query.message.reply_text("⚙️ <b>Pilih Aksi / Kualitas Download:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("prev:"):
        await query.answer()
        viewkey = data.split(":", 1)[1]
        video_info = VIDEO_CACHE.get(viewkey)
        if video_info and video_info.get("preview"):
            status_prev = await query.message.reply_text("⏳ Memuat preview...", parse_mode="HTML")
            media_bytes, ext = await fetch_media_bytes(video_info["preview"])
            
            if media_bytes:
                file_obj = io.BytesIO(media_bytes)
                file_obj.name = f"preview_{viewkey}.{ext if ext else 'webm'}"
                caption_text = f"🎞 <b>Preview Teaser</b>\n{html.escape(video_info['title'])}"

                sent = False
                try:
                    await query.message.reply_video(video=file_obj, caption=caption_text, parse_mode="HTML")
                    sent = True
                except Exception:
                    file_obj.seek(0)

                if not sent:
                    try:
                        await query.message.reply_animation(animation=file_obj, caption=caption_text, parse_mode="HTML")
                        sent = True
                    except Exception:
                        file_obj.seek(0)

                if not sent:
                    try:
                        await query.message.reply_document(document=file_obj, caption=caption_text, parse_mode="HTML")
                        sent = True
                    except Exception as e:
                        logging.error(f"Gagal kirim preview: {e}")

                if sent:
                    await status_prev.delete()
                else:
                    await status_prev.edit_text("❌ Format media preview tidak didukung oleh Telegram.")
            else:
                await status_prev.edit_text("❌ Gagal mengunduh preview.")
        else:
            await query.message.reply_text("❌ Teaser preview tidak tersedia untuk video ini.")

    elif data.startswith("rel:"):
        await query.answer()
        viewkey = data.split(":", 1)[1]
        status_msg = await query.message.reply_text("🔗 Mencari **Video Serupa**...", parse_mode="Markdown")
        videos = await scrape_related_videos(viewkey, limit=5)
        if not videos:
            await status_msg.edit_text("❌ Tidak ditemukan video serupa.")
            return

        text_reply, reply_markup = build_search_response(videos, "Video Serupa", page=1)
        await status_msg.edit_text(text_reply, parse_mode="HTML", reply_markup=reply_markup, link_preview_options=LinkPreviewOptions(is_disabled=True))

    elif data.startswith("fav_add:"):
        viewkey = data.split(":", 1)[1]
        video_info = VIDEO_CACHE.get(viewkey, {"title": f"Video {viewkey}", "duration": "-", "viewkey": viewkey})
        if add_favorite(user_id, video_info):
            await query.answer("✅ Berhasil disimpan ke Favorit Saya!", show_alert=False)
        else:
            await query.answer("ℹ️ Video ini sudah ada di dalam Favorit Anda.", show_alert=False)

    elif data.startswith("fav_del:"):
        viewkey = data.split(":", 1)[1]
        remove_favorite(user_id, viewkey)
        await query.answer("🗑 Berhasil dihapus dari Favorit.", show_alert=False)

    # --- FITUR BATALKAN DOWNLOAD ---
    elif data.startswith("cancel_dl:"):
        task_id = data.split(":", 1)[1]
        CANCEL_DOWNLOAD_TASKS.add(task_id)
        await query.answer("🛑 Membatalkan pengunduhan...", show_alert=True)
        try:
            await query.message.edit_text("🛑 <b>Pengunduhan telah dibatalkan oleh pengguna.</b>", parse_mode="HTML")
        except Exception:
            pass

    # --- PROSES DOWNLOAD REAL-TIME ---
    elif data.startswith("dl:"):
        await query.answer()
        _, viewkey, quality = data.split(":")
        target_url = f"https://www.pornhub.com/view_video.php?viewkey={viewkey}"
        
        task_id = f"{user_id}_{viewkey}_{int(time.time())}"
        if task_id in CANCEL_DOWNLOAD_TASKS:
            CANCEL_DOWNLOAD_TASKS.remove(task_id)

        cancel_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Batalkan Download", callback_data=f"cancel_dl:{task_id}")]
        ])

        status_msg = await query.message.reply_text("⏳ Memulai pengunduhan...", parse_mode="HTML", reply_markup=cancel_markup)
        ext = "mp3" if quality == "mp3" else "mp4"
        filename = f"video_{viewkey}_{quality}.{ext}"

        loop = asyncio.get_running_loop()
        success, direct_stream_url = await asyncio.to_thread(
            download_video_file, target_url, filename, quality, task_id, loop, status_msg
        )

        if direct_stream_url == "CANCELLED":
            if os.path.exists(filename):
                os.remove(filename)
            if task_id in CANCEL_DOWNLOAD_TASKS:
                CANCEL_DOWNLOAD_TASKS.remove(task_id)
            return

        if success and os.path.exists(filename):
            await status_msg.edit_text("📤 Memeriksa dan menyiapkan file...")
            
            files_to_send = split_video_file(filename, max_size_mb=45)
            
            try:
                for idx, part_file in enumerate(files_to_send, start=1):
                    caption = (
                        f"🎬 <b>Part {idx}/{len(files_to_send)}</b> ({quality}p)\n🔗 {target_url}"
                        if len(files_to_send) > 1
                        else f"🎬 <b>Video ({quality}p) Berhasil Diunduh!</b>\n🔗 {target_url}"
                    )
                    
                    with open(part_file, "rb") as file_data:
                        if quality == "mp3":
                            await query.message.reply_audio(audio=file_data, caption=caption, parse_mode="HTML")
                        else:
                            await query.message.reply_video(video=file_data, caption=caption, parse_mode="HTML")
                    
                    if part_file != filename and os.path.exists(part_file):
                        os.remove(part_file)

                await status_msg.delete()
            except Exception as e:
                logging.error(f"Error sending file: {e}")
                await status_msg.edit_text("❌ Gagal mengunggah file ke Telegram.")
            finally:
                if os.path.exists(filename):
                    os.remove(filename)
                if task_id in CANCEL_DOWNLOAD_TASKS:
                    CANCEL_DOWNLOAD_TASKS.remove(task_id)
        else:
            reply_markup = None
            if direct_stream_url and direct_stream_url != "CANCELLED":
                reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Stream Video Langsung", url=direct_stream_url)]])

            await status_msg.edit_text(
                "❌ Gagal mendownload langsung ke Telegram.\nAnda dapat menontonnya via Stream Link berikut:",
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            if task_id in CANCEL_DOWNLOAD_TASKS:
                CANCEL_DOWNLOAD_TASKS.remove(task_id)

# ---------------------------------------------------------
# 7. Handler Inline Mode
# ---------------------------------------------------------
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return

    videos = await scrape_pornhub(query, page=1, limit=5)
    results = []

    for v in videos:
        safe_title = html.escape(v["title"])
        content = (
            f"🎬 <b>{safe_title}</b>\n"
            f"⏱ Durasi: {v['duration']} | 👁 Views: {v['views']} | ⭐ {v['rating']}\n"
            f"🔗 <a href=\"{v['url']}\">Tonton Video</a>"
        )
        thumb_url = v.get("thumb") or v.get("preview") or None
        results.append(
            InlineQueryResultArticle(
                id=v["viewkey"],
                title=v["title"],
                description=f"⏱ {v['duration']} | ⭐ {v['rating']} | 👁 {v['views']}",
                thumbnail_url=thumb_url,
                input_message_content=InputTextMessageContent(message_text=content, parse_mode="HTML")
            )
        )

    await update.inline_query.answer(results, cache_time=60)

# ---------------------------------------------------------
# 8. Main Loop dengan Web Server aiohttp
# ---------------------------------------------------------
async def handle_ping(request):
    return web.Response(text="Bot is running!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("phsearch", search_handler))
    app.add_handler(CommandHandler("search", search_handler))
    
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_handler))

    port = int(os.environ.get("PORT", 8080))
    web_app = web.Application()
    web_app.router.add_get("/", handle_ping)
    runner = web.AppRunner(web_app)
    
    async def start_web_server():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_web_server())

    print(f"🤖 Bot Telegram Siap Dijalankan di Port {port}!")
    app.run_polling()

if __name__ == "__main__":
    main()
