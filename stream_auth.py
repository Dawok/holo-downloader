import logging
from typing import Callable, Optional


def needs_cookies(error: Exception) -> bool:
    """Inspect the original error when livestream_dl wraps yt-dlp failures."""
    seen = set()
    while id(error) not in seen:
        seen.add(id(error))
        original = error.__cause__ or error.__context__
        if original is None or id(original) in seen:
            break
        error = original
    message = str(error).lower().replace('’', "'")
    return any(text in message for text in (
        'sign in', 'log in', 'login required', 'authentication required',
        'not a bot', 'not a robot', 'captcha', 'anti-bot',
        'members-only', 'members only', 'membership', 'join this channel',
        'channel members', "channel's members",
        'subscriber-only', 'subscriber_only', 'premium video', 'youtube premium',
        'age-restricted', 'age restricted', 'confirm your age',
        'private video', 'video is private',
    ))


def ytdlp_cookie_options(options: Optional[dict], cookies: Optional[str]) -> dict:
    """Keep custom yt-dlp options from bypassing the stream's cookie policy."""
    return {**(options or {}), 'cookiefile': cookies, 'cookiesfrombrowser': None}


class CookieSession:
    """Use cookies for members or an authentication retry, scoped to one stream."""

    def __init__(self, cookies_file: Optional[str], logger: logging.Logger,
                 members_only: bool = False, cancelled: Callable[[], bool] = lambda: False):
        self.cookies_file = cookies_file
        self.logger = logger
        self.use_cookies = members_only
        self.cancelled = cancelled

    @property
    def cookies(self) -> Optional[str]:
        return self.cookies_file if self.use_cookies else None

    def observe_info(self, info: dict) -> None:
        if info.get('availability') in ('subscriber_only', 'premium_only'):
            self.use_cookies = True

    def run(self, operation: Callable):
        try:
            return operation(self.cookies)
        except Exception as error:
            if self.use_cookies or not self.cookies_file or self.cancelled() or not needs_cookies(error):
                raise
            self.use_cookies = True
            self.logger.info('YouTube requires authentication; retrying this stream with cookies')
            return operation(self.cookies)
