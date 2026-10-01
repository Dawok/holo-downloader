import common
import os
import threading
import time
import sqlite3
import random
import secrets
import hmac
import shutil
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from flask import Flask, abort, render_template, request, redirect, send_file, send_from_directory, url_for, flash, make_response, jsonify, session
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.job import Job
from apscheduler.triggers.cron import CronTrigger
import tomlkit
from channel_config import (CONFIG_LOCK, read_config, write_config,
                            channels_from_config, empty_channel, save_channel,
                            delete_channel, channel_url, CHANNEL_TABLES)

from getConfig import ConfigHandler, config_file_path
import downloadVid
import getMembers
import communityPosts
import unarchived
import getVids

from flask_caching import Cache

import re

import queue

# Set stack size to 1MB (1024*1024 bytes)
# Note: This applies to all new threads created subsequently.
try:
    threading.stack_size(1024 * 1024) 
except ValueError:
    pass # Some platforms have strict page size requirements

# --- Configuration & Constants ---
config_file_path = 'config.toml'
DB_FILE = os.environ.get('HISTORY_DB', 'stream_history.db')
THUMBNAIL_MIME_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.webp': 'image/webp', '.gif': 'image/gif', '.avif': 'image/avif',
}
LOCK = threading.Lock()

scheduler = BackgroundScheduler(daemon=True)

# Global State for Active Downloads
active_downloads = {}
active_unarchived_downloads = {}
other_threads = {}

# NEW: Global State for signaling history update from a background thread
recently_finished = [] 

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-placeholder')

# Configure SimpleCache (stores in RAM)
app.config['CACHE_TYPE'] = 'SimpleCache' 
app.config['CACHE_DEFAULT_TIMEOUT'] = 300 # Default 5 minutes

cache = Cache(app)

GLOBAL_THEME = "dark"

history_update_event = threading.Event()


def get_display_timezone():
    try:
        return ZoneInfo(os.environ.get('TZ') or 'UTC')
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


@app.template_filter('app_datetime')
def app_datetime(value):
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    # SQLite's CURRENT_TIMESTAMP is UTC without an offset.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(get_display_timezone())


@app.context_processor
def shared_ui_context():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return {'csrf_token': session['csrf_token'], 'theme': session.get('theme', GLOBAL_THEME)}


@app.before_request
def protect_form_requests():
    if request.method == 'POST':
        expected = session.get('csrf_token', '')
        provided = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
        if not expected or not hmac.compare_digest(expected.encode(), provided.encode()):
            if request.path.startswith('/api/'):
                return jsonify(error='Your session expired. Reload the page and try again.'), 400
            return render_template('error.html', message='Your session expired. Reload the page and try again.'), 400


@app.template_filter('status_class')
def status_class(value):
    status = str(value or '').lower()
    if 'error' in status or 'failed' in status:
        return 'danger'
    if 'warning' in status or 'waiting' in status:
        return 'warning'
    if status in {'finished', 'recording', 'scheduled'}:
        return 'success'
    if status in {'monitoring', 'get chat', 'muxing', 'moving'}:
        return 'info'
    return 'muted'

# --- Database Management ---

def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id VARCHAR(11),
                type VARCHAR(20),
                status VARCHAR(20),
                total_size INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(history)')}
        for column, column_type in (('title', 'TEXT'), ('channel', 'TEXT'),
                                    ('thumbnail', 'BLOB'), ('thumbnail_mime_type', 'TEXT'),
                                    ('error_message', 'TEXT')):
            if column not in columns:
                conn.execute(f'ALTER TABLE history ADD COLUMN {column} {column_type}')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS removed_streams (
                video_id TEXT PRIMARY KEY,
                removed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

def is_stream_removed(video_id):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        return conn.execute('SELECT 1 FROM removed_streams WHERE video_id = ?', (video_id,)).fetchone() is not None

def suppress_stream(video_id):
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.execute('INSERT OR IGNORE INTO removed_streams (video_id) VALUES (?)', (video_id,))

def allow_stream(video_id):
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.execute('DELETE FROM removed_streams WHERE video_id = ?', (video_id,))

def save_to_history(video_id, stats, download_type="Unknown", info=None, thumbnail_path=None):
    """Saves finished stream to DB and ensures only last 50 exist."""
    thumbnail = thumbnail_mime_type = None
    if thumbnail_path:
        path = Path(thumbnail_path)
        mime_type = THUMBNAIL_MIME_TYPES.get(path.suffix.lower())
        if mime_type:
            try:
                thumbnail = path.read_bytes() or None
                thumbnail_mime_type = mime_type if thumbnail else None
            except OSError:
                app.logger.warning('Could not read archived thumbnail for %s', video_id)
    # Use context manager for auto-closing
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        c = conn.cursor()
        
        # Safe access to nested dicts using .get with defaults
        vid_size = stats.get('video', {}).get("current_filesize", 0) or 0
        aud_size = stats.get('audio', {}).get("current_filesize", 0) or 0
        download_size = vid_size + aud_size

        info = info or {}
        c.execute('''INSERT INTO history
                     (video_id, type, total_size, status, title, channel, thumbnail, thumbnail_mime_type, error_message)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (video_id, download_type, download_size, stats.get("status", None),
                   info.get('fulltitle') or info.get('title'),
                   info.get('channel') or info.get('uploader') or info.get('author_name'),
                   thumbnail, thumbnail_mime_type, stats.get('error_message')))
        
        # Cleanup old history
        c.execute('''
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history ORDER BY id DESC LIMIT 50
            )
        ''')
        conn.commit()

def get_history():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        # We execute directly on the connection for brevity
        rows = conn.execute('''SELECT id, video_id, type, status, total_size, timestamp, title, channel,
                               thumbnail IS NOT NULL AS has_thumbnail, error_message
                               FROM history ORDER BY id DESC''').fetchall()
        return rows

# --- Config Management (Unchanged from previous update) ---

def load_config():
    if not os.path.exists(config_file_path):
        doc = tomlkit.document()
        doc.add("app_name", "StreamArchiver")
        
        doc.add(tomlkit.comment("Format: Minute Hour Day_of_Month Month Day_of_Week (standard 5 fields)"))
        doc.add("cron_schedule", "*/30 * * * *") 
        
        with open(config_file_path, "w") as f:
            f.write(tomlkit.dumps(doc))
            
    return ConfigHandler(config_file=config_file_path)

def save_config(content, expected_revision):
    try:
        write_config(config_file_path, content, expected_revision)
        return True, "Config saved"
    except Exception as e:
        return False, str(e)

def get_download_metadata(downloader):
    info = downloader.info_dict
    embed = downloader.embed_info
    return {
        'fulltitle': info.get('fulltitle'),
        'title': info.get('title') or embed.get('title'),
        'channel': info.get('channel') or info.get('uploader') or embed.get('author_name'),
        'release_timestamp': info.get('release_timestamp'),
        'live_status': info.get('live_status')
    }

def get_download_thumbnail(downloader):
    """Find the saved thumbnail, including files moved out of the temporary folder."""
    file_names = getattr(downloader.livestream_downloader, 'file_names', {})
    thumbnail = file_names.get('thumbnail')
    if not thumbnail:
        return None
    thumbnail = Path(thumbnail)
    output = getattr(downloader, 'thumbnail_output', None)
    candidates = [Path(f'{output}{thumbnail.suffix}'), thumbnail] if output else [thumbnail]
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None

def _iter_paths(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_paths(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _iter_paths(child)
    elif isinstance(value, (str, os.PathLike)):
        yield Path(value)

def _temp_cleanup_root(config):
    configured = Path(config.get_temp_folder()).expanduser()
    parts = configured.parts
    template_index = next((index for index, part in enumerate(parts) if "%(" in part), None)
    root = Path(*parts[:template_index]) if template_index is not None else configured
    return root.resolve()

def remove_download_temp_files(video_id, downloader):
    try:
        root = _temp_cleanup_root(downloader.config)
    except Exception:
        common.logger.exception("Could not resolve temporary folder for %s", video_id)
        return

    temp_output_dir = getattr(downloader, 'temp_output_dir', None)
    candidate = Path(temp_output_dir).expanduser() if temp_output_dir else None
    candidate_root = None
    removed_folder_parent = None
    try:
        if candidate is not None and not candidate.is_symlink():
            candidate = candidate.resolve()
            relative_parts = candidate.relative_to(root).parts
            if candidate != root:
                candidate_root = candidate
                if candidate.is_dir() and any(video_id in part for part in relative_parts):
                    shutil.rmtree(candidate)
                    removed_folder_parent = candidate.parent
    except (OSError, ValueError):
        common.logger.exception("Could not remove temporary folder for %s", video_id)
        candidate_root = None

    if removed_folder_parent is not None:
        parent = removed_folder_parent
        try:
            while parent != root:
                parent.relative_to(root)
                parent.rmdir()
                parent = parent.parent
        except (OSError, ValueError):
            pass

    file_names = getattr(getattr(downloader, 'livestream_downloader', None), 'file_names', {})
    paths = list(_iter_paths(file_names))
    for path in paths:
        try:
            resolved = path.resolve()
            relative_parts = resolved.relative_to(root).parts
            if candidate_root is not None:
                resolved.relative_to(candidate_root)
            elif not any(video_id in part for part in relative_parts):
                continue
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue

    for path in paths:
        try:
            parent = path.resolve().parent
            while parent != root:
                parent.relative_to(root)
                parent.rmdir()
                parent = parent.parent
        except (OSError, ValueError):
            continue

# --- Core Logic & Threading ---

def thread_worker(video_id, downloader, thread_tracker: dict = active_downloads):
    global recently_finished
    remove_requested = False
    try:
        try:
            downloader.main()
        except Exception as error:
            stats = downloader.livestream_downloader.stats
            stats['status'] = 'Error'
            stats['error_message'] = f'{type(error).__name__}: {error}'
            common.logger.exception("Error downloading %s", video_id)
        with LOCK:
            entry = thread_tracker.get(video_id)
            owns_entry = entry is not None and entry.get('downloader') is downloader
            remove_requested = owns_entry and entry.get('remove_requested', False)
            download_type = entry.get('type', 'Unknown') if owns_entry else 'Unknown'
            if owns_entry:
                entry['worker_finished'] = True

        if not remove_requested:
            save_to_history(video_id, downloader.livestream_downloader.stats,
                            download_type=download_type,
                            info=get_download_metadata(downloader),
                            thumbnail_path=get_download_thumbnail(downloader))

            with app.app_context():
                cache.delete('history-fragment')

            history_update_event.set()
            
    except Exception:
        common.logger.exception("Error downloading %s", video_id)
    finally:
        with LOCK:
            entry = thread_tracker.get(video_id)
            owns_entry = entry is not None and entry.get('downloader') is downloader
            remove_requested = owns_entry and entry.get('remove_requested', False)
            if owns_entry:
                entry['worker_finished'] = True

        if remove_requested:
            remove_download_temp_files(video_id, downloader)

        with LOCK:
            entry = thread_tracker.get(video_id)
            if entry is not None and entry.get('downloader') is downloader:
                thread_tracker.pop(video_id, None)
        
        # REMOVED: gc.collect() 
        # (Let Python manage this. Only run manual GC on a schedule if absolutely necessary)

def start_download(video_id, manual=False):
    if isinstance(video_id, dict):
        id = video_id.get('id', None) or video_id.get('video_id', None) # added option in case additional fields are included in future
    else:
        id = video_id
    with LOCK:
        if id in active_downloads:
            return False 
        if manual:
            allow_stream(id)
        elif is_stream_removed(id):
            common.logger.info("Skipping manually removed stream %s", id)
            return False

        downloader = downloadVid.VideoDownloader(id=video_id, config=load_config())
        thread = threading.Thread(target=thread_worker, args=(id, downloader, active_downloads), daemon=True)
        
        active_downloads[id] = {
            'downloader': downloader,
            'thread': thread,
            'type': "stream",
            'start_time': datetime.now(timezone.utc)
        }
        
        thread.start()
        return True
    
def start_unarchived_download(video_id):
    with LOCK:
        if video_id in active_unarchived_downloads or is_stream_removed(video_id):
            return False 

        downloader = unarchived.UnarchivedDownloader(id=video_id, config=load_config())
        thread = threading.Thread(target=thread_worker, args=(video_id, downloader, active_unarchived_downloads), daemon=True)
        
        active_unarchived_downloads[video_id] = {
            'downloader': downloader,
            'thread': thread,
            'type': "unarchived",
            'start_time': datetime.now(timezone.utc)
        }
        
        thread.start()
        return True
"""
def get_streams():
    common.logger.info("Running scheduled stream check...")
    streams = getVids.main(unarchived=False)
    for stream in streams:
        start_download(stream)
        # Have slightly randomised stream to help prevent rate limiting
        if len(streams) > 1:
            time.sleep(random.uniform(5.0, 10.0))

def get_unarchived():
    common.logger.info("Running scheduled stream check...")
    streams = getVids.main(unarchived=True)
    for stream in streams:
        start_unarchived_download(stream)
        # Have slightly randomised stream to help prevent rate limiting
        if len(streams) > 1:
            time.sleep(random.uniform(5.0, 10.0))

def get_members():
    common.logger.info("Running scheduled stream check...")
    streams = getMembers.main()
    for stream in streams:
        start_download(stream)
        # Have slightly randomised stream to help prevent rate limiting
        if len(streams) > 1:
            time.sleep(random.uniform(5.0, 10.0))
"""
def get_videos_with_queue(discovery_func, download_func, *args, **kwargs):
    """
    Helper to run discovery in a thread and process items from a queue.
    """
    stream_queue = queue.Queue()
    common.logger.info("Running scheduled stream check...")

    # 1. Start discovery in a background thread
    # Note: discovery_func (like getVids.main) must be updated to accept 'q'
    discovery_thread = threading.Thread(
        target=discovery_func, 
        args=args, 
        kwargs={'queue': stream_queue, **kwargs},
        daemon=True
    )
    discovery_thread.start()

    # 2. Process items as they arrive
    while True:
        try:
            # Wait for an item (timeout ensures we check if thread is still alive)
            stream = stream_queue.get(timeout=1)
            
            download_func(stream)            
            
            stream_queue.task_done()

            # Spread out download starts
            time.sleep(random.uniform(5.0, 10.0))
        except queue.Empty:
            # If queue is empty and discovery is done, we are finished
            if not discovery_thread.is_alive():
                break

def get_streams():
    get_videos_with_queue(getVids.main, start_download, unarchived=False, return_dict=True, config=load_config())

def get_unarchived():
    get_videos_with_queue(getVids.main, start_unarchived_download, unarchived=True, return_dict=False, config=load_config())

def get_members():
    get_videos_with_queue(getMembers.main, start_download, return_dict=True, config=load_config())

def get_community_tab():
    common.logger.info("Running scheduled stream check...")
    communityPosts.main(config=load_config())

def update_scheduler():
    config = load_config()
    
    def create_schedule(name: str, method):
        schedule_name = f"{name}-checker"
        if scheduler.get_job(schedule_name):
            scheduler.remove_job(schedule_name)

        cron_string = config.get_cron_schedule(name)
        if not cron_string:
            return False, f"No schedule for {name}"
        
        try:
            trigger = CronTrigger.from_crontab(cron_string)
            
            scheduler.add_job(
                method, 
                trigger=trigger,
                id=schedule_name,
                max_instances=1,      # <-- Prevent overlapping checks
                coalesce=True,        # <-- If missed run, run once only
                replace_existing=True # <-- Ensure old job is overwritten
            )
            return True, f"Scheduler updated with: {cron_string}"
        except ValueError as e:
            common.logger.error(f"Cron Error: {e}")
            return False, f"Invalid Cron expression: {e}"

    schedules = {
        "streams": get_streams,
        "unarchived": get_unarchived,
        "members_only": get_members,
        "community_posts": get_community_tab,
    }

    for schedule_type, method in schedules.items():
        created, message = create_schedule(name=schedule_type, method=method)
        if created is False:
            common.logger.debug("Error creating schedule for {0}: {1}".format(schedule_type, message))
    return True, "All valid schedules loaded"


# --- Helper for HTMX Data Routes ---

def get_job_timing(job, info, is_waiting=False):
    """Read stream timestamps without starting a clock when the page is opened."""
    released_at = None
    if info.get('release_timestamp') is not None:
        try:
            released_at = datetime.fromtimestamp(float(info['release_timestamp']), timezone.utc)
        except (OverflowError, OSError, TypeError, ValueError):
            pass

    queued_at = job['start_time']
    started_at = None if is_waiting else (released_at or job.get('recording_start_time') or queued_at)
    scheduled_at = released_at if is_waiting else None
    date_format = '%b %d, %Y at %H:%M %Z'
    return {
        'start_time': (started_at or queued_at).astimezone(get_display_timezone()).strftime('%H:%M:%S'),
        'start_timestamp': started_at.timestamp() if started_at else None,
        'start_datetime': started_at.astimezone(timezone.utc).isoformat() if started_at else None,
        'elapsed': elapsed_time(started_at) if started_at else None,
        'scheduled_timestamp': scheduled_at.timestamp() if scheduled_at else None,
        'scheduled_datetime': scheduled_at.isoformat() if scheduled_at else None,
        'scheduled_start': scheduled_at.astimezone(get_display_timezone()).strftime(date_format).replace(' 0', ' ') if scheduled_at else None,
    }

def get_active_jobs_data():
    """Prepares active download data for display."""
    with LOCK:
        current_jobs = []
        for vid, job in active_downloads.copy().items():
            if job.get('remove_requested'):
                continue
            downloader: downloadVid.VideoDownloader = job['downloader']
            display_info = get_download_metadata(downloader)
            stats = downloader.livestream_downloader.stats
            status = str(stats.get('status') or '').strip().lower()
            is_waiting = status.startswith('waiting')
            is_recording = status == 'recording'

            current_jobs.append({
                'id': vid,
                'stats': stats,
                'info': display_info, # Pass the small dict, not the huge one
                'is_waiting': is_waiting,
                'is_recording': is_recording,
                'queued_timestamp': job['start_time'].timestamp(),
                **get_job_timing(job, display_info, is_waiting),
            })
    current_jobs.sort(key=lambda job: (
        0 if job['is_recording'] else 1 if job['is_waiting'] else 2,
        -job['start_timestamp'] if job['is_recording'] else (
            job['scheduled_timestamp'] if job['is_waiting'] and job['scheduled_timestamp'] is not None
            else float('inf') if job['is_waiting'] else job['queued_timestamp']),
        job['id']))
    return current_jobs

def get_active_unarchived_jobs_data():
    """Prepares active download data for display."""
    with LOCK:
        current_jobs = []
        for vid, job in active_unarchived_downloads.copy().items():
            downloader: downloadVid.VideoDownloader = job['downloader']
            display_info = get_download_metadata(downloader)
            stats = downloader.livestream_downloader.stats
            status = str(stats.get('status') or '').strip().lower()
            is_waiting = status.startswith('waiting')

            current_jobs.append({
                'id': vid,
                'stats': stats,
                'info': display_info, # Pass the small dict, not the huge one
                'is_waiting': is_waiting,
                'is_recording': status == 'recording',
                **get_job_timing(job, display_info, is_waiting),
            })
    return current_jobs

def elapsed_time(started):
    seconds = max(0, int(time.time() - started.timestamp()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}'

# --- Helper to format byte strings ---
def convert_bytes(bytes):
        try:
            int(float(bytes))
        except Exception:
            common.logger.exception("Error converting {0} to number".format(bytes))
            return "Invalid Value"
        # List of units in order
        units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB']
        
        # Start with bytes and convert to larger units
        unit_index = 0
        while bytes >= 1024 and unit_index < len(units) - 1:
            bytes /= 1024
            unit_index += 1
        
        # Format and return the result
        return f"{bytes:.2f} {units[unit_index]}"

app.jinja_env.filters['convert_bytes'] = convert_bytes

# --- HTMX Data Routes ---

@app.route('/data/active')
def data_active():
    """Endpoint for HTMX to poll active downloads table AND signal history update."""
    current_jobs = get_active_jobs_data()
    # 1. Render the active table
    rendered_table = render_template('active_table.html', active_downloads=current_jobs)
    response = make_response(rendered_table)
    
    # 2. Check the signal list set by the background thread
    with LOCK:
        if history_update_event.is_set():
            # Send HTMX trigger header, instructing the client to fire 'historyUpdated' event
            response.headers['HX-Trigger'] = 'historyUpdated' 
            history_update_event.clear() # Reset the flag
            
    return response

@app.route('/data/unarchived')
def data_unarchived():
    """Endpoint for HTMX to poll active downloads table AND signal history update."""
    current_jobs = get_active_unarchived_jobs_data()
    # 1. Render the active table
    rendered_table = render_template('unarchived_active_table.html', active_downloads=current_jobs)
    response = make_response(rendered_table)
    
    # 2. Check the signal list set by the background thread
    with LOCK:
        if history_update_event.is_set():
            # Send HTMX trigger header, instructing the client to fire 'historyUpdated' event
            response.headers['HX-Trigger'] = 'historyUpdated' 
            history_update_event.clear() # Reset the flag
            
    return response

@app.route('/data/history')
@cache.cached(timeout=30, key_prefix='history-fragment')
def data_history():
    """Endpoint for HTMX to poll history table."""
    history = get_history()
    return render_template('history_table.html', history=history)

@app.route('/history/<int:history_id>/thumbnail')
def history_thumbnail(history_id):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        row = conn.execute('SELECT thumbnail, thumbnail_mime_type FROM history WHERE id = ?',
                           (history_id,)).fetchone()
    if not row or not row[0] or row[1] not in THUMBNAIL_MIME_TYPES.values():
        abort(404)
    return send_file(BytesIO(row[0]), mimetype=row[1], max_age=86400)

@app.route('/data/recent')
def data_recent():
    return render_template('history_table.html', history=get_history()[:5], recent=True)


# --- Web Routes (Unchanged) ---

@app.route('/')
def index():
    history = get_history()
    active_jobs = get_active_jobs_data()
    
    return render_template('index.html', 
                                  history=history,
                                  active_downloads=active_jobs,
                                  monitors=get_active_unarchived_jobs_data(),
                                  channel_count=len(channels_from_config(read_config(config_file_path)[0])),
                                  ) 

@app.route('/actions/check', methods=['POST'])
def manual_check():
    """Triggers an existing job's function to run immediately."""

    manual_check_id = "manual-stream-check"

    # 1. Get the existing job object
    job = scheduler.get_job(manual_check_id)

    if job:
        common.logger.warning("Manual stream check already running")
        flash("Manual stream check already triggered, please wait for existing check to finish", "warning")
        return redirect(url_for('index'))

    # 2. Schedule a new one-off job with the same function/parameters
    scheduler.add_job(
        get_streams, 
        trigger='date',
        run_date=datetime.now(),
        id=manual_check_id,
        max_instances=1,
        coalesce=True,
        replace_existing=True
        # Copying job stores, executor, etc. is often unnecessary
        # but you might want to specify executor if you need a different one.
    )
    common.logger.debug(f"Successfully triggered immediate run for job ID: {manual_check_id}")
    flash("Manual check triggered! Tables will update shortly.", "success")
    return redirect(url_for('index'))



def extract_youtube_id(url: str):
    """
    Extracts the YouTube video ID from various URL formats.
    Returns the ID string if found, otherwise None.
    """

    url = url.strip()

    # Check if the input is ALREADY a valid 11-character ID
    # ^ and $ ensure the entire string matches, not just a part of it
    if re.match(r'^[a-zA-Z0-9_-]{11}$', url):
        return url
    # Regex pattern to capture the 11-character ID
    # Handles: youtube.com/watch?v=, youtu.be/, youtube.com/embed/, 
    # youtube.com/shorts/, and youtube.com/live/
    pattern = r'(?:https?:\/\/)?(?:www\.|m\.)?(?:youtube\.com\/(?:watch\?v=|embed\/|shorts\/|live\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    
    match = re.search(pattern, url)
    
    return match.group(1) if match else None

@app.route('/actions/add', methods=['POST'])
def manual_add():
    video_id = request.form.get('video_id')
    if video_id:
        video_id = extract_youtube_id(str(video_id))
        if not video_id:
            flash('Enter a valid YouTube video link or 11-character video ID.', 'danger')
            return redirect(url_for('index'))
        if start_download(video_id, manual=True):
            flash(f"Started download for {video_id}", "success")
        else:
            with LOCK:
                entry = active_downloads.get(video_id, {})
                removing = entry.get('remove_requested', False)
                downloader = entry.get('downloader')
                check_requested = (downloader is not None and not removing
                                   and not entry.get('worker_finished')
                                   and downloader.request_live_check())
                recording = (downloader is not None
                             and str(downloader.livestream_downloader.stats.get('status') or '').strip().lower() == 'recording')
            if removing:
                flash(f"Video {video_id} is being removed. Try adding it again after cleanup finishes.", "warning")
            elif check_requested:
                flash(f"Stream {video_id} is already added and waiting to start. Checking whether it is live now.", "success")
            elif recording:
                flash(f"Video {video_id} is already recording.", "warning")
            else:
                flash(f"Video {video_id} is already in progress.", "warning")
    return redirect(url_for('index'))

@app.route('/config', methods=['GET', 'POST'])
def config_page():
    doc, config_revision = read_config(config_file_path)
    content = tomlkit.dumps(doc)
    if request.method == 'POST':
        content = request.form.get('toml_content', '')
        success, message = save_config(content, request.form.get('revision', ''))
        if success:
            success, message = update_scheduler()
            if success:
                load_config()
                flash(message, "success")                
            else:
                flash(message, "danger") 
            return redirect(url_for('config_page'))
        return render_template('config.html', config_content=content,
                               revision=request.form.get('revision', ''), error=message), 400
    return render_template('config.html', config_content=content, revision=config_revision)


@app.route('/channels')
def channels_page():
    doc, config_revision = read_config(config_file_path)
    channels = channels_from_config(doc)
    counts = {mode: sum(channel[mode] for channel in channels) for mode in CHANNEL_TABLES}
    return render_template('channels.html', channels=channels, counts=counts,
                           revision=config_revision, query=request.args.get('q', ''),
                           mode=request.args.get('mode', 'all'))


@app.route('/channels/new', methods=['GET', 'POST'])
@app.route('/channels/<channel_id>/edit', methods=['GET', 'POST'])
def channel_editor(channel_id=None):
    doc, config_revision = read_config(config_file_path)
    channels = channels_from_config(doc)
    channel = next((item for item in channels if item['id'] == channel_id), None)
    if channel_id and not channel:
        abort(404)
    channel = channel or {**empty_channel(), 'public': True}
    error = None
    if request.method == 'POST':
        channel = dict(request.form)
        channel['id'] = channel_id or request.form.get('id', '')
        for mode in CHANNEL_TABLES:
            channel[mode] = request.form.get(mode) == 'on'
        config_revision = request.form.get('revision', '')
        try:
            saved = save_channel(config_file_path, channel, config_revision, editing=channel_id is not None)
            flash(f"Saved {saved['name']}. New settings apply to the next channel check.", 'success')
            return redirect(url_for('channels_page'))
        except (ValueError, OSError) as exception:
            error = str(exception)
    return render_template('channel_editor.html', channel=channel, channels=channels,
                           editing=channel_id is not None, revision=config_revision,
                           error=error), 400 if error else 200


@app.route('/channels/<channel_id>/delete', methods=['POST'])
def channel_delete(channel_id):
    try:
        delete_channel(config_file_path, channel_id, request.form.get('revision', ''))
        flash('Channel removed from future checks. Existing recordings are kept.', 'success')
    except (ValueError, OSError) as exception:
        flash(str(exception), 'danger')
    return redirect(url_for('channels_page'))


@app.route('/api/channels/resolve', methods=['POST'])
def resolve_channel():
    import yt_dlp
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get('source'), str):
        return jsonify(error='Enter a channel link, @handle, or ID.'), 400
    try:
        url = channel_url(payload['source'])
    except ValueError as exception:
        return jsonify(error=str(exception)), 400
    options = {'quiet': True, 'no_warnings': True, 'extract_flat': True,
               'playlist_items': '1', 'skip_download': True, 'socket_timeout': 10,
               'retries': 0, 'extractor_retries': 0}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        channel_id = (info or {}).get('channel_id') or (info or {}).get('id', '')
        name = (info or {}).get('channel') or (info or {}).get('uploader') or (info or {}).get('title')
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel_id) or not name:
            raise ValueError('Channel details unavailable')
        doc, _ = read_config(config_file_path)
        exists = any(channel['id'] == channel_id for channel in channels_from_config(doc))
        return jsonify(id=channel_id, name=name,
                       edit_url=url_for('channel_editor', channel_id=channel_id) if exists else None)
    except Exception:
        return jsonify(error='Could not look up this channel. Try again, or enter its name and channel ID below.'), 502


@app.route('/activity')
def activity_page():
    return render_template('activity.html', history=get_history())


@app.route('/schedules', methods=['GET', 'POST'])
def schedules_page():
    doc, config_revision = read_config(config_file_path)
    schedules = dict(doc.get('cron_schedule', {}))
    error = None
    if request.method == 'POST':
        config_revision = request.form.get('revision', '')
        names = ('streams', 'unarchived', 'members_only', 'community_posts')
        schedules = {name: request.form.get(name, '').strip() for name in names}
        try:
            for value in schedules.values():
                if value:
                    CronTrigger.from_crontab(value)
            with CONFIG_LOCK:
                current_doc, _ = read_config(config_file_path)
                if 'cron_schedule' not in current_doc:
                    current_doc['cron_schedule'] = tomlkit.table()
                for name, value in schedules.items():
                    if value:
                        current_doc['cron_schedule'][name] = value
                    else:
                        current_doc['cron_schedule'].pop(name, None)
                write_config(config_file_path, tomlkit.dumps(current_doc), config_revision)
            update_scheduler()
            flash('Schedules updated.', 'success')
            return redirect(url_for('schedules_page'))
        except (ValueError, OSError) as exception:
            error = str(exception)
    return render_template('schedules.html', jobs=get_scheduler_jobs(), schedules=schedules,
                           revision=config_revision, error=error), 400 if error else 200

@app.route('/actions/cancel/<video_id>', methods=['POST'])
def cancel_download(video_id):
    with LOCK:
        if video_id in active_downloads:
            job_entry = active_downloads[video_id]
            downloader_instance: downloadVid.VideoDownloader = job_entry.get('downloader')
            if job_entry.get('worker_finished'):
                flash(f"Stream {video_id} has already finished.", "secondary")
                return redirect(url_for('index'))
            suppression_saved = True
            try:
                suppress_stream(video_id)
            except sqlite3.Error as e:
                suppression_saved = False
                common.logger.error("Could not suppress removed stream %s: %s", video_id, e)
            try:
                downloader_instance.kill_this.set()
                job_entry['remove_requested'] = True
                if suppression_saved:
                    flash(f"Removing stream {video_id} and its temporary files. It will stay skipped until you add it manually.", "success")
                else:
                    flash(f"Removing stream {video_id}, but its scheduled-scan exclusion could not be saved.", "warning")
            except Exception as e:
                common.logger.error(f"Failed to cancel {video_id}: {e}")
                flash(f"Could not remove {video_id}.", "danger")
                e = None
        else:
            flash(f"Stream {video_id} is not currently active.", "secondary")
            
    return redirect(url_for('index'))

@app.route('/actions/cancel_unarchived/<video_id>', methods=['POST'])
def cancel_unarchived(video_id):
    """Sets the kill flag to True for a specific downloader."""
    with LOCK:
        if video_id in active_unarchived_downloads:
            job_entry = active_unarchived_downloads[video_id]
            downloader_instance: downloadVid.VideoDownloader = job_entry.get('downloader')
            
            # Navigate to the inner downloader object that holds the flag
            # Based on your existing code: downloader -> livestream_downloader
            try:
                downloader_instance.kill_this.set()
            except Exception as e:
                common.logger.error(f"Failed to cancel {video_id}: {e}")
                flash(f"Error cancelling {video_id}", "danger")
                e = None
        else:
            flash(f"Stream {video_id} is not currently active.", "secondary")
            
    return redirect(url_for('index'))

@app.route('/actions/toggle_theme', methods=['POST'])
def toggle_theme():
    session['theme'] = 'dark' if session.get('theme', GLOBAL_THEME) == 'light' else 'light'
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify(theme=session['theme'])
    return_to = request.form.get('return_to', '/')
    return redirect(return_to if return_to.startswith('/') and not return_to.startswith('//') else '/')

# --- Helper for Scheduler Data ---

def get_scheduler_jobs():
    """Extracts all possible jobs and their current status."""
    # Define the 'Master List' of jobs your app supports
    possible_jobs = [
        {"id": "streams-checker", "display": "Stream Checker"},
        {"id": "unarchived-checker", "display": "Unarchived Checker"},
        {"id": "members_only-checker", "display": "Members Only Checker"},
        {"id": "community_posts-checker", "display": "Community Tab Checker"},
    ]
    active_jobs = {job.id: job for job in scheduler.get_jobs()}
    final_data = []

    for item in possible_jobs:
        job_id = item["id"]
        job_obj: Job = active_jobs.get(job_id)
        
        if job_obj:
            next_run = job_obj.next_run_time.strftime('%H:%M:%S (%d-%b)') if job_obj.next_run_time else "Paused"
            trigger = str(job_obj.trigger)
            status = "Scheduled"
        else:
            next_run = "N/A"
            trigger = "No Schedule Set"
            status = "Inactive"
            
        final_data.append({
            'id': job_id,
            'name': item["display"],
            'trigger': trigger,
            'next_run': next_run,
            'status': status
        })
    return final_data

@app.route('/actions/force_run/<job_name>', methods=['POST'])
def force_run_job(job_name):
    """Executes a job function immediately in the background."""
    # Mapping job IDs back to their functions
    mapping = {
        "streams-checker": get_streams,
        "unarchived-checker": get_unarchived,
        "members_only-checker": get_members,
        "community_posts-checker": get_community_tab,
    }
    manual_check_id = f"manual-{job_name}"
    func = mapping.get(job_name)
    if func:
        job = scheduler.get_job(manual_check_id)
        if job:
            common.logger.warning("Manual stream check already running")
            flash("Manual stream check already triggered, please wait for existing check to finish", "warning")
        else:    
            # Run it immediately as a one-off job
            scheduler.add_job(func, 
                              trigger='date', 
                              run_date=datetime.now(), 
                              id=manual_check_id, name=manual_check_id, 
                              max_instances=1,      # <-- Prevent overlapping checks
                              coalesce=True,        # <-- If missed run, run once only
                              replace_existing=True # <-- Ensure old job is overwritten
                            )
            common.logger.debug(f"Successfully triggered immediate run for job ID: {manual_check_id}")
            flash(f"Force-run triggered for {job_name}", "info")
    else:
        flash(f"Unknown job: {job_name}", "danger")
        
    return redirect(url_for('schedules_page'))

# --- HTMX Route for Scheduler ---

@app.route('/data/scheduler')
def data_scheduler():
    """Endpoint for HTMX to poll scheduler status."""
    jobs = get_scheduler_jobs()
    # Note: If you moved templates to files, use render_template('scheduler_table.html', ...)
    return render_template('schedule_table.html', jobs=jobs)

# --- JSON API Endpoints ---

@app.route('/api/active')
def api_active():
    """JSON equivalent of the active stream downloads."""
    # get_active_jobs_data returns the raw 'stats' dict containing bytes
    # It does format the start_time to HH:MM:SS string, but keeps stats raw.
    data = get_active_jobs_data()
    return jsonify(data)

@app.route('/api/unarchived')
def api_unarchived():
    """JSON equivalent of the active unarchived downloads."""
    data = get_active_unarchived_jobs_data()
    return jsonify(data)

@app.route('/api/history')
def api_history():
    """JSON equivalent of the download history."""
    rows = get_history()
    
    # SQLite Rows are not directly JSON serializable, convert to dict.
    # The 'total_size' field in DB is INTEGER (bytes), so no conversion needed.
    history_data = [dict(row) for row in rows]
    
    return jsonify(history_data)

@app.route('/api/scheduler')
def api_scheduler():
    """JSON equivalent of the scheduler status."""
    # Returns the list of jobs with their next run times and status
    data = get_scheduler_jobs()
    return jsonify(data)


@app.route('/favicon.ico')
def favicon():
    try:
        return make_response(send_from_directory('static', 'favicon.ico'))
    except FileNotFoundError:
        abort(404)


# ---------------------------------------------------------
# 1. Determine Config Path
# ---------------------------------------------------------
if __name__ == '__main__':
    import argparse
    # CASE A: Local Development (python web.py)
    # We use argparse so you can use flags like --config
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, default="config.toml")
    args = parser.parse_args()
    config_file_path = args.config
    print("Running in Developer Mode")

else:
    # CASE B: Production (Waitress / Docker)
    # We use Environment Variables because we can't pass args to an import
    config_file_path = os.environ.get('CONFIG_FILE', 'config.toml')

# ---------------------------------------------------------
# 2. Common Initialization (Runs in BOTH cases)
# ---------------------------------------------------------
# Now that we know where the config is, we load it and start the app
# regardless of how it was launched.

common.setup_umask()
config: ConfigHandler = load_config() # Use the path we determined above
GLOBAL_THEME = config.get_webui_theme()

init_db()

update_scheduler()
scheduler.start()

# ---------------------------------------------------------
# 3. Start Local Server
# ---------------------------------------------------------
if __name__ == '__main__':
    # This only runs if called directly. Waitress ignores this.
    app.run(host='0.0.0.0', port=5000, load_dotenv=True)
