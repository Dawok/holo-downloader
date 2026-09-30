#!/usr/local/bin/python
import yt_dlp
import os
import threading
from getConfig import ConfigHandler
from pathlib import Path

import discord_web
from time import sleep, asctime, time
from random import uniform
# Import FileLock, setup_umask, AND the shared kill_all event from common
from common import FileLock, setup_umask, kill_all, initialize_logging

import argparse
import logging
from typing import Union, Optional, Tuple, Dict, Any

from livestream_dl import download_Live,getUrls
import httpx

import json

'''
# --- Logging Initialization Helper (Define locally for modularity) ---

def initialize_logging(config: ConfigHandler, logger_name: Optional[str] = None) -> logging.Logger:
    """Initializes logging based on the provided ConfigHandler instance."""
    from livestream_dl.download_Live import setup_logging
    name = logger_name if logger_name else __name__
    return setup_logging(
        log_level=config.get_log_level(), 
        console=True, 
        file=config.get_log_file(), 
        file_options=config.get_log_file_options(),
        logger_name=name
    )
'''
# --- Core Functions Updated with Dependencies ---
class VideoDownloader():
    def __init__(self, id, config: ConfigHandler = None, logger: logging.Logger = None, kill_this: threading.Event = None):
        if isinstance(id, dict):
            self.id = id.get('id', None) or id.get('video_id', None) # added option in case additional fields are included in future
            self.channel_id = id.get('channel_id', None)
        else:
            self.id = id
            self.channel_id = None

        if not self.id:
            raise ValueError("No video ID provided, unable to continue")
        
        self.kill_this: threading.Event = kill_this or threading.Event()

        if config is None:
            config = ConfigHandler()
        self.config: ConfigHandler = config

        if logger is None:
            logger = initialize_logging(config, logger_name="Downloader", video_id=self.id)
        self.logger: logging.Logger = logger
        
        self.livestream_downloader = download_Live.LiveStreamDownloader(kill_all=kill_all, logger=logger, kill_this=self.kill_this)
        

        self.info_dict = {}
        self.outputFile = None
        self.temp_output_dir = None

        try:
            response = httpx.get("https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={0}".format(self.id), timeout=30)
            self.embed_info: Optional[Dict[str, Any]] = response.json() if response.status_code == 200 else {}
            self.embed_info.pop("html", None)
        except Exception as e:
            self.logger.warning(f"Could not fetch oembed info: {e}")
            self.embed_info = {}
        

    def createTorrent(self, output: str) -> None:
        import subprocess
        """Creates a torrent file for the given output path using the provided config."""
        if not self.config.getTorrent():
            return
        fullPath = self.config.getTempOutputPath(output)
        folder = Path(fullPath).parent
        
        # Use config methods to build the command
        subprocess.run(self.config.torrentBuilder(fullPath, folder), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            
    def downloader(self, info_dict: Dict[str, Any]) -> None:
        """
        Handles the main video segment download process.
        """
        if not info_dict or self.outputFile is None:
            raise Exception(("Unable to retrieve information about video {0}".format(self.id)))

        # Options retrieved using the passed config object
        options: dict = self.config.get_livestream_dl_options(info_dict=info_dict, output_template=self.outputFile)
        self.thumbnail_output = self.livestream_downloader.output_filename(info_dict, options["output"])
        
        # Start additional information downloaders (Discord notification)
        # NOTE: Assuming discord_web.main is updated to accept the config object
        discord_notify = threading.Thread(target=discord_web.main, kwargs={"id": self.id, "status": "recording", "config": self.config, "logger": self.logger}, daemon=True)
        discord_notify.start() 
        
        try:            
            self.livestream_downloader.stats["status"] = "Recording"
            try:
                self.livestream_downloader.download_segments(info_dict=info_dict, resolution=options.get("resolution"), options=options)
            finally:
                self.temp_output_dir = options.get("temp_folder")
            
            if self.kill_this.is_set():
                self.livestream_downloader.stats["status"] = "Cancelled"
            else:
                self.livestream_downloader.stats["status"] = "Finished"
        except KeyboardInterrupt as e:
            self.livestream_downloader.stats["status"] = "Cancelled"
            self.logger.warning("Download of {0} was cancelled".format(self.id))
            return
        except Exception as e:
            self.logger.exception("Error occured {0}".format(self.id))
            self.livestream_downloader.stats["status"] = "Error"
            self.livestream_downloader.stats["error_message"] = f"{type(e).__name__}: {e}"
            sleep(1.0)
            raise Exception(("{3} - Error downloading video: {0}, {1}: {2}".format(self.id, type(e).__name__, e, asctime())))
        finally:
            # Wait for remaining processes, up to 60s
            discord_notify.join(timeout=60.0)
            
        # Final notification using the config object
        discord_web.main(id=self.id, status="done", config=self.config)
        return

    def download_video_info(self, video_url: str) -> Tuple[str, Dict[str, Any]]:
        """
        Fetches video metadata and prepares the output file template.
        
        Uses the passed config and logger objects.
        """
        options = {
            'outtmpl': self.config.get_ytdlp(self.channel_id),
            'quiet': True,
            'no_warnings': True      
        }
        
        
        
        max_wait = max(float(self.config.upcoming_video_max_wait()), 60)
        while True:
            if self.kill_this.is_set():
                raise InterruptedError("Download was removed")
            additional_ytdlp_options = json.loads(self.config.get_ytdlp_options() or "{}")
            additional_ytdlp_options.setdefault("socket_timeout", 30)
            additional_ytdlp_options["logger"] = self.logger
            info_dict, live_status = getUrls.get_Video_Info(
                id=video_url,
                wait=False,
                cookies=self.config.get_cookies_file(),
                proxy=self.config.get_proxy(),
                additional_options=additional_ytdlp_options,
                include_dash=self.config.get_include_dash(),
                include_m3u8=self.config.get_include_m3u8(),
                clean_info_dict=self.config.get_clean_info_json(),
                ignore_no_formats=True,
                logger=self.logger,
            )
            if self.kill_this.is_set():
                raise InterruptedError("Download was removed")
            self.info_dict = {
                'id': info_dict.get('id'),
                'title': info_dict.get('title'),
                'fulltitle': info_dict.get('fulltitle'),
                'uploader': info_dict.get('uploader'),
                'thumbnail': info_dict.get('thumbnail'),
                'webpage_url': info_dict.get('webpage_url')
            }
            if live_status != "is_upcoming":
                break
            release_timestamp = info_dict.get("release_timestamp")
            wait_seconds = (release_timestamp - time() if release_timestamp is not None
                            else uniform(60, max_wait))
            if self.kill_this.wait(min(max(wait_seconds, 60), max_wait)):
                raise InterruptedError("Download was removed")

        with yt_dlp.YoutubeDL(options) as ydl:
            outputFile = str(ydl.prepare_filename(info_dict)).replace("%", "％")
                
        self.logger.debug("({0}) Info.json: {1}".format(video_url, json.dumps(info_dict)))
        self.logger.info("Output file: {0}".format(outputFile))

        return outputFile, info_dict
        
    def main(self, use_lock_file=False) -> None:
        """
        Main function for handling video download with file locking.
        """
        
        def run_download():
            # Use the config object for pre-download notification
            discord_web.main(self.id, "waiting", config=self.config)
            self.livestream_downloader.stats["status"] = "Waiting"
            try:
                # Pass config and logger
                self.outputFile, info_dict = self.download_video_info(self.id)
                self.logger.debug("Output file: {0}".format(self.outputFile))

                # Create distilled info_dict for web ui
                self.info_dict = {
                    'id': info_dict.get('id'),
                    'title': info_dict.get('title'),
                    'fulltitle': info_dict.get('fulltitle'),
                    'uploader': info_dict.get('uploader'),
                    'thumbnail': info_dict.get('thumbnail'),
                    'webpage_url': info_dict.get('webpage_url'),
                    'release_timestamp': info_dict.get('release_timestamp'),
                    'live_status': info_dict.get('live_status')
                }
                
                if self.outputFile is None:
                    raise LookupError(("Unable to retrieve information about video {0}".format(id)))
                
                # Pass config and logger
                self.downloader(info_dict)
                
            except Exception as e:
                if self.kill_this.is_set():
                    self.livestream_downloader.stats["status"] = "Cancelled"
                    self.logger.info("Download of %s was removed", self.id)
                    return
                self.livestream_downloader.stats["status"] = "Error"
                self.livestream_downloader.stats.setdefault("error_message", f"{type(e).__name__}: {e}")
                self.logger.exception("Error downloading video")
                # Pass config for error notification
                discord_web.main(self.id, "error", message=f"{type(e).__name__}: {str(e)}"[-500:], config=self.config)

        
        if use_lock_file:
            # Run "run_download" within lock file
            if os.path.exists("/dev/shm/"):
                    lock_file_path = "/dev/shm/videoDL-{0}".format(self.id)
            else:
                lock_file_path = os.path.join(self.config.get_temp_folder(), "videoDL-{0}.lockfile".format(self.id))
            try:
                
                with FileLock(lock_file_path) as lock_file:
                    lock_file.acquire()
                    
                    run_download()

                    lock_file.release()
            except (IOError, BlockingIOError) as e:
                self.logger.info("Unable to acquire lock for {0}, must be already downloading: {1}".format(lock_file_path, e))
        else:
            run_download()

if __name__ == "__main__":
    try:
        # 1. Setup umask globally
        setup_umask() 

        # 2. Instantiate ConfigHandler once
        app_config = ConfigHandler()
        
        # We will initialize the logger inside main for the video ID, but need a general one for errors here
        # Initialize a logger that will catch initial parsing/execution errors
        
    
    
        # Create the parser
        parser = argparse.ArgumentParser(description="Process an video by ID")
        parser.add_argument('ID', type=str, help='The video ID (required)')

        # Parse the arguments
        args = parser.parse_args()

        main_logger = initialize_logging(config=app_config, logger_name=f"{args.ID}")

        downloader = VideoDownloader(id=args.ID, config=app_config, logger=main_logger)
        # Call main, passing the config object and the logger
        downloader.main(use_lock_file=True)
        
    except Exception as e:
        # Use the initialized logger for final error handling
        logging.exception("An unhandled error occurred when attempting to download a video")

        raise

