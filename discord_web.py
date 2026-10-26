
import html
import httpx
import sys
from discord_webhook import DiscordWebhook, DiscordEmbed
from getConfig import ConfigHandler
import logging
import re
from typing import Optional, Any, Dict
from common import initialize_logging

def send_webhook(url: str, id: str = "Unknown", status: str = "error", message: Optional[str] = None, config: ConfigHandler = None, logger: logging = None, info: Optional[Dict[str, Any]] = None):
    if url is None:
        return
    
    if config is None:
        config = ConfigHandler()
    
    if message is not None:
        message = remove_ansi_escape_sequences(message)

    if logger is None:
        initialize_logging(config, "discord")
        
    webhook = DiscordWebhook(url, rate_limit_retry=True, timeout=30)
    
    if(status == "starting"):
        title="Starting"
        color="fc8803"
        embed = DiscordEmbed(title, description="Starting holo-downloader", color=color)
        embed.set_timestamp()
        webhook.add_embed(embed)
        webhook.execute()
        return
    elif(status=="membership-error"):
        title = "Membership error"
        color="ff0000"
        embed = DiscordEmbed(title, description=("Error checking membership streams for [{0}](https://www.youtube.com/channel/{0}). \nCheck cookies!".format(id)), color=color)
        if message:
            embed.add_embed_field(name="Error Message: {0}".format(id), value=message)
            embed.set_footer(text='Error Logger')
        embed.set_timestamp()
        webhook.add_embed(embed)
        webhook.execute()
        return
    elif(status=="playlist-error"):
        title = "Playlist error"
        color="ff0000"
        embed = DiscordEmbed(title, description=("Error checking streams playlist for [{0}](https://www.youtube.com/channel/{0}). \nCheck cookies!".format(id)), color=color)
        if message:
            embed.add_embed_field(name="Error Message: {0}".format(id), value=message)
            embed.set_footer(text='Error Logger')
        embed.set_timestamp()
        webhook.add_embed(embed)
        webhook.execute()
        return
    
    embed_error: Optional[str] = None
    # Prefer metadata already fetched by yt-dlp. YouTube's oEmbed omits the
    # title, channel and thumbnail for live and upcoming streams.
    response_data: Optional[Dict[str, Any]] = video_details(info)
    if response_data is None:
        try:
            response = httpx.get("https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={0}".format(id), timeout=30)
            response_data = oembed_details(response.json()) if response.status_code == 200 else None
        except Exception as e:
            embed_error = str(e)
            response_data = None
        
    color="03b2f8"  

    match status:
        case "recording":
            title = "Recording"
            color="0011ff"
        case "waiting":
            title = "Waiting"
            color="ffff00"
        case "error":
            title = "Error"
            color="ff0000"
        case "done":
            title = "Done"
            color="00ff00"
        case _:
            title = "Notification" # Default for unhandled status
            color="03b2f8"

    # Check if the request was successful
    if embed_error is None and response_data is not None:
        video_title = response_data.get('title')
        description = "[{0}](https://youtu.be/{1})".format(video_title, id) if video_title else "https://youtu.be/{0}".format(id)
        embed = DiscordEmbed(title, description=description, color=color)
        if response_data.get('author_name'):
            embed.set_author(name=response_data.get('author_name'), url=response_data.get('author_url'))
        if response_data.get('thumbnail_url'):
            embed.set_thumbnail(url=response_data.get('thumbnail_url'))
        
    elif embed_error:
        embed = DiscordEmbed(title, description="Video https://youtu.be/{0} is not accessible due to error: {1}".format(id, embed_error[:250]), color=color)
    else:                    
        embed = DiscordEmbed(title, description="Video https://youtu.be/{0} is not accessible, perhaps it has been privated".format(id), color=color)

    if message:
        embed.add_embed_field(name="Message: {0}".format(id), value=message)
        
    embed.set_timestamp()
    webhook.add_embed(embed)
    webhook.execute()
    
def video_details(info: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Maps a yt-dlp info dict to the oEmbed field names used by the embed."""
    # yt-dlp appends the start time to live stream titles; fulltitle omits it.
    title = info and (info.get('fulltitle') or info.get('title'))
    if not title:
        return None
    return {
        'title': title,
        'author_name': info.get('channel') or info.get('uploader'),
        'author_url': info.get('channel_url') or info.get('uploader_url'),
        'thumbnail_url': info.get('thumbnail'),
    }

def oembed_details(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fills a missing oEmbed title from the player iframe's title attribute."""
    if not data.get('title'):
        match = re.search(r'title="([^"]*)"', data.get('html') or '')
        if match and match.group(1):
            data['title'] = html.unescape(match.group(1))
    return data

def remove_ansi_escape_sequences(text: str) -> str:
    # This regular expression matches ANSI escape sequences
    ansi_escape = re.compile(r'\x1B[@-_][0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

def main(id: str, status: str, message: Optional[str] = None, config: ConfigHandler = None, logger: logging = None, info: Optional[Dict[str, Any]] = None):
    """
    Handles fetching the Discord webhook URL from config and sending the webhook.
    
    :param id: The video ID.
    :param status: The status (e.g., 'recording', 'done', 'error').
    :param message: Optional message to include in the embed.
    :param config: The ConfigHandler object, defaults to a new instance if None.
    :param info: Optional yt-dlp info dict used instead of oEmbed for the video details.
    """
    # Instantiate ConfigHandler if it's not provided
    if config is None:
        config = ConfigHandler()

    if logger is None:
        initialize_logging(config, "discord")

    webhook_url = config.get_discord_webhook()
    
    if webhook_url is not None:
        send_webhook(webhook_url, id, status, message, logger=logger, info=info)


if __name__ == "__main__":
    try:
        # Create the Config Handler instance
        app_config = ConfigHandler()
        
        # Initialize logging using the instance
        logger = initialize_logging(app_config, "discord") 
    
    
        # Determine if a message argument was passed
        message_arg = sys.argv[3] if len(sys.argv) > 3 else None
        
        # Pass the config object to main
        main(sys.argv[1], sys.argv[2], message=message_arg, config=app_config, logger=logger)
        
    except IndexError:
        logging.error("Usage: python script.py <video_id> <status> [message]")
        sys.exit(1)
    except Exception as e:
        logging.exception("An unhandled error occurred when trying to send a message to Discord")
        raise
