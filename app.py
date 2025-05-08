from flask import Flask, request, jsonify, send_file, render_template, Response, stream_with_context
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import yt_dlp
import asyncio
import os
import aiohttp
import uuid
import threading
import time
from collections import deque
from urllib.parse import urlparse
import re
import mimetypes
from tenacity import retry, stop_after_attempt, wait_exponential
import subprocess
import logging
import json

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RAW_FOLDER = "downloads/raw"
CLEANED_FOLDER = "downloads/cleaned"
os.makedirs(RAW_FOLDER, exist_ok=True)
os.makedirs(CLEANED_FOLDER, exist_ok=True)

logs = deque(maxlen=100)
task_status = {}  # Dictionary to track task status
task_queue = asyncio.Queue(maxsize=100)  # Limit queue size
MAX_WORKERS = 1  # Reduced to 1 to minimize memory usage
TASK_TIMEOUT = 300  # 5 minutes timeout for tasks
cleaned_files = set()  # Track files scheduled for cleanup

def log(msg, user_friendly=True):
    # Remove emojis for internal logging, keep for user-facing logs
    msg = msg.strip()
    print(msg)
    if user_friendly:
        logs.append(msg)
    logger.info(msg)

def cleanup_file(file_path):
    time.sleep(30)  # Delay to ensure file is no longer in use
    try:
        if os.path.exists(file_path):
            mime_type, _ = mimetypes.guess_type(file_path)
            if mime_type and (mime_type.startswith('video/') or mime_type.startswith('audio/')):
                os.remove(file_path)
                log("File cleaned up 🗑️", user_friendly=True)
            else:
                log(f"Skipped non-video/audio file: {file_path}", user_friendly=False)
        else:
            log(f"File not found for cleanup: {file_path}", user_friendly=False)
    except Exception as e:
        log(f"Error removing file {file_path}: {str(e)}", user_friendly=False)
    finally:
        cleaned_files.discard(file_path)

def is_valid_url(url):
    regex = re.compile(
        r'^(https?://)?((www\.)?(tiktok\.com|vt\.tiktok\.com|instagram\.com|youtube\.com|youtu\.be))(/.*)?$',
        re.IGNORECASE
    )
    return bool(regex.match(url))

def detect_platform(url):
    url = url.lower()
    if "tiktok.com" in url or "vt.tiktok.com" in url:
        log("Detected TikTok video", user_friendly=True)
        return "tiktok"
    elif "instagram.com" in url:
        log("Detected Instagram video", user_friendly=True)
        return "instagram"
    elif "youtube.com" in url or "youtu.be" in url:
        log("Detected YouTube video", user_friendly=True)
        return "youtube"
    return "unknown"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def clean_with_adarsus(video_path):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            log("Cleaning video metadata", user_friendly=True)
            await page.goto("https://adarsus.com/en/remove-metadata-online-document-image-video/")
            await page.set_input_files("input#fileDialog", video_path)
            await page.wait_for_selector("button#removeMetadata", timeout=60000)
            await page.click("button#removeMetadata")

            log("Processing metadata cleanup", user_friendly=True)
            async with page.expect_download(timeout=120000) as download_info:
                download = await download_info.value
            cleaned_id = str(uuid.uuid4())
            cleaned_path = os.path.join(CLEANED_FOLDER, f"{cleaned_id}.mp4")
            await download.save_as(cleaned_path)
            log("Metadata cleanup completed", user_friendly=True)

            await browser.close()
            return True, cleaned_id
    except PlaywrightTimeoutError:
        log("Timeout during metadata cleanup", user_friendly=True)
        return False, "Timeout while processing on Adarsus"
    except Exception as e:
        log(f"Error during metadata cleanup: {str(e)}", user_friendly=True)
        return False, str(e)

def download_with_ytdlp(url, platform, quality):
    raw_id = str(uuid.uuid4())
    ext = "mp4" if quality != "audio" else "m4a"
    raw_path = os.path.join(RAW_FOLDER, f"{raw_id}.{ext}")
    ydl_opts = {
        'outtmpl': raw_path,
        'noplaylist': True,
        'quiet': False,
        'no_warnings': False,
    }

    if quality == "audio":
        ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio'
        ydl_opts['merge_output_format'] = 'm4a'
    elif quality == "mute":
        ydl_opts['format'] = 'bestvideo[ext=mp4]/best[ext=mp4]'
        ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4'}]
    else:  # auto
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        ydl_opts['merge_output_format'] = 'mp4'

    if platform == "instagram":
        ydl_opts.update({
            '--user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            '--cookies-from-browser': 'chrome',
        })
    elif platform == "youtube":
        ydl_opts.update({
            '--no-check-certificate': True,
            '--geo-bypass': True,
        })

    try:
        log(f"Downloading {platform.capitalize()} video", user_friendly=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        log("Download completed", user_friendly=True)
        return True, raw_path
    except yt_dlp.utils.DownloadError as e:
        log(f"Failed to download video: {str(e)}", user_friendly=True)
        return False, str(e)
    except Exception as e:
        log(f"Error downloading video: {str(e)}", user_friendly=True)
        return False, str(e)

async def process_video(url, remove_metadata, quality, task_id):
    platform = detect_platform(url)
    task_status[task_id] = {"ready": False, "error": None, "link": None, "queued": True, "status": "queued"}

    try:
        if platform == "tiktok":
            if remove_metadata:
                success, result = await download_and_clean_tiktok(url, quality)
            else:
                success, result = await download_and_skip_clean_tiktok(url, quality)
        elif platform in ["youtube", "instagram"]:
            success, raw_path_or_error = download_with_ytdlp(url, platform, quality)
            if not success:
                raise Exception(raw_path_or_error)
            if remove_metadata:
                success, result = await clean_with_adarsus(raw_path_or_error)
            else:
                final_id = str(uuid.uuid4())
                ext = "mp4" if quality != "audio" else "m4a"
                cleaned_path = os.path.join(CLEANED_FOLDER, f"{final_id}.{ext}")
                os.rename(raw_path_or_error, cleaned_path)
                log("Video saved without metadata cleanup", user_friendly=True)
                success, result = True, final_id
        else:
            raise Exception("Unsupported platform")

        if success:
            task_status[task_id] = {"ready": True, "error": None, "link": f"/download/{result}", "queued": False, "status": "completed"}
        else:
            task_status[task_id] = {"ready": False, "error": result, "queued": False, "status": "failed"}
    except Exception as e:
        task_status[task_id] = {"ready": False, "error": str(e), "queued": False, "status": "failed"}
        log(f"Error processing video: {str(e)}", user_friendly=True)

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=4, max=10))
async def download_and_clean_tiktok(url, quality):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            log("Connecting to TikTok download service", user_friendly=True)
            await page.goto("https://snaptik.app/en")
            await page.fill("input[name='url']", url)
            await page.click("button[type='submit']")

            log("Preparing TikTok video", user_friendly=True)
            await page.wait_for_selector("a[href*='snaptik.app/file']", timeout=60000)

            if quality == "audio":
                log("TikTok audio not supported, downloading video", user_friendly=True)
                selector = "a[href*='snaptik.app/file']"
            else:
                selector = "a[href*='snaptik.app/file']"

            download_link = await page.locator(selector).first.get_attribute("href")
            if not download_link:
                raise Exception("Download link not found")

            log("TikTok video link found", user_friendly=True)

            log("Downloading TikTok video", user_friendly=True)
            async with aiohttp.ClientSession() as session:
                async with session.get(download_link) as response:
                    raw_path = os.path.join(RAW_FOLDER, "tiktok_raw.mp4")
                    with open(raw_path, "wb") as f:
                        f.write(await response.read())
            log("TikTok video downloaded", user_friendly=True)

            await browser.close()

            if quality == "mute":
                log("Removing TikTok audio 🔇", user_friendly=True)
                muted_path = os.path.join(RAW_FOLDER, "tiktok_muted.mp4")
                subprocess.run([
                    "ffmpeg", "-i", raw_path, "-an", "-c:v", "copy", muted_path
                ], check=True)
                os.remove(raw_path)
                raw_path = muted_path

            return await clean_with_adarsus(raw_path)
    except PlaywrightTimeoutError:
        log("Timeout fetching TikTok video", user_friendly=True)
        return False, "Timeout while fetching TikTok download link"
    except Exception as e:
        log(f"Error downloading TikTok video: {str(e)}", user_friendly=True)
        return False, str(e)

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=4, max=10))
async def download_and_skip_clean_tiktok(url, quality):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()

            log("Connecting to TikTok download service", user_friendly=True)
            await page.goto("https://snaptik.app/en")
            await page.fill("input[name='url']", url)
            await page.click("button[type='submit']")

            log("Preparing TikTok video", user_friendly=True)
            await page.wait_for_selector("a[href*='snaptik.app/file']", timeout=60000)

            if quality == "audio":
                log("TikTok audio not supported, downloading video", user_friendly=True)
                selector = "a[href*='snaptik.app/file']"
            else:
                selector = "a[href*='snaptik.app/file']"

            download_link = await page.locator(selector).first.get_attribute("href")
            if not download_link:
                raise Exception("Download link not found")

            log("TikTok video link found", user_friendly=True)

            log("Downloading TikTok video", user_friendly=True)
            async with aiohttp.ClientSession() as session:
                async with session.get(download_link) as response:
                    final_id = str(uuid.uuid4())
                    temp_path = os.path.join(RAW_FOLDER, f"tiktok_temp.mp4")
                    with open(temp_path, "wb") as f:
                        f.write(await response.read())
            log("TikTok video downloaded", user_friendly=True)

            await browser.close()

            if quality == "mute":
                log("Removing TikTok audio 🔇", user_friendly=True)
                final_id = str(uuid.uuid4())
                cleaned_path = os.path.join(CLEANED_FOLDER, f"{final_id}.mp4")
                subprocess.run([
                    "ffmpeg", "-i", temp_path, "-an", "-c:v", "copy", cleaned_path
                ], check=True)
                os.remove(temp_path)
            else:
                final_id = str(uuid.uuid4())
                cleaned_path = os.path.join(CLEANED_FOLDER, f"{final_id}.mp4")
                os.rename(temp_path, cleaned_path)

            return True, final_id
    except PlaywrightTimeoutError:
        log("Timeout fetching TikTok video", user_friendly=True)
        return False, "Timeout while fetching TikTok download link"
    except Exception as e:
        log(f"Error downloading TikTok video: {str(e)}", user_friendly=True)
        return False, str(e)

async def worker(worker_id):
    log(f"Worker {worker_id} started", user_friendly=False)
    while True:
        try:
            task = await asyncio.wait_for(task_queue.get(), timeout=10)
            log(f"Worker {worker_id} processing task {task['task_id']}", user_friendly=False)
            await asyncio.sleep(1)  # Add delay to prevent memory spikes
            await process_video(task["url"], task["remove_metadata"], task["quality"], task["task_id"])
            task_queue.task_done()
            log(f"Worker {worker_id} completed task {task['task_id']}", user_friendly=False)
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            log(f"Worker {worker_id} error processing task: {str(e)}", user_friendly=False)
            task_queue.task_done()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except Exception as e:
        log(f"Background loop error: {str(e)}", user_friendly=False)

def initialize_workers():
    try:
        loop = asyncio.new_event_loop()
        threading.Thread(target=start_background_loop, args=(loop,), daemon=True).start()
        for i in range(MAX_WORKERS):
            asyncio.run_coroutine_threadsafe(worker(i+1), loop)
        log("Workers initialized successfully", user_friendly=False)
    except Exception as e:
        log(f"Failed to initialize workers: {str(e)}", user_friendly=False)
        raise

# Initialize workers at app startup
with app.app_context():
    initialize_workers()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/clean', methods=['POST'])
async def clean_video():
    url = request.form.get('url')
    remove_meta = request.form.get('remove_metadata') == 'true'
    quality = request.form.get('quality', 'auto')

    if not url:
        return jsonify({'success': False, 'error': 'Missing URL'})

    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url

    if not is_valid_url(url):
        return jsonify({'success': False, 'error': 'Invalid URL format'})

    task_id = str(uuid.uuid4())
    try:
        await task_queue.put({"url": url, "remove_metadata": remove_meta, "quality": quality, "task_id": task_id})
        log("Task enqueued for processing", user_friendly=True)
        return jsonify({'success': True, 'video_id': task_id})
    except asyncio.QueueFull:
        log("Queue full, try again later", user_friendly=True)
        return jsonify({'success': False, 'error': 'Queue full, try again later'})

@app.route('/stream')
def stream():
    def event_stream():
        for msg in logs:
            yield f"data: {msg}\n\n"
        last_sent = len(logs)
        while True:
            if len(logs) > last_sent:
                for msg in list(logs)[last_sent:]:
                    yield f"data: {msg}\n\n"
                last_sent = len(logs)
            time.sleep(0.1)
    return Response(stream_with_context(event_stream()), mimetype='text/event-stream')

@app.route('/download_status/<vid>')
def download_status(vid):
    def status_stream():
        start_time = time.time()
        while time.time() - start_time < TASK_TIMEOUT:
            status = task_status.get(vid, {"ready": False, "error": None, "link": None, "queued": True, "status": "queued"})
            yield f"data: {json.dumps(status)}\n\n"
            if status["ready"] or status["error"]:
                break
            time.sleep(1)
        if time.time() - start_time >= TASK_TIMEOUT:
            task_status[vid] = {"ready": False, "error": "Timeout waiting for download", "queued": False, "status": "failed"}
            yield f"data: {json.dumps(task_status[vid])}\n\n"
    return Response(stream_with_context(status_stream()), mimetype='text/event-stream')

@app.route('/download/<vid>')
def download_file(vid):
    filename_mp4 = f"{vid}.mp4"
    filename_m4a = f"{vid}.m4a"
    path_mp4 = os.path.normpath(os.path.join(CLEANED_FOLDER, filename_mp4))
    path_m4a = os.path.normpath(os.path.join(CLEANED_FOLDER, filename_m4a))
    
    if os.path.exists(path_mp4):
        path = path_mp4
        mimetype = 'video/mp4'
        download_name = 'video.mp4'
    elif os.path.exists(path_m4a):
        path = path_m4a
        mimetype = 'audio/mp4'
        download_name = 'audio.m4a'
    else:
        return "File not found", 404

    if not path.startswith(os.path.normpath(CLEANED_FOLDER)):
        return "Invalid file path", 403

    if path not in cleaned_files:
        cleaned_files.add(path)
        threading.Thread(target=cleanup_file, args=(path,)).start()

    return send_file(path, mimetype=mimetype, as_attachment=True, download_name=download_name)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)