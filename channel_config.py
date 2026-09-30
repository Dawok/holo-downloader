import hashlib
import os
import re
import threading
from urllib.parse import urlparse

import tomlkit
from tomlkit.items import KeyType, SingleKey


CONFIG_LOCK = threading.RLock()
CHANNEL_TABLES = {
    "public": "channel_ids_to_match",
    "members": "members_only",
    "unarchived": "unarchived_channel_ids_to_match",
    "community": "community_tab",
}
RULE_TABLES = {
    "title_regex": "title_filter",
    "description_regex": "description_filter",
    "output_template": "per_channel_output_template",
}


class ConfigConflict(ValueError):
    pass


def revision(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_config(path):
    with CONFIG_LOCK:
        with open(path, encoding="utf-8", newline="") as config_file:
            content = config_file.read()
        return tomlkit.parse(content), revision(content)


def write_config(path, content, expected_revision):
    tomlkit.parse(content)
    with CONFIG_LOCK:
        with open(path, "r+", encoding="utf-8", newline="") as config_file:
            current = config_file.read()
            if revision(current) != expected_revision:
                raise ConfigConflict("Configuration changed in another window. Reload this page before saving again.")
            config_file.seek(0)
            config_file.write(content)
            config_file.truncate()
            config_file.flush()
            os.fsync(config_file.fileno())


def canonical_id(value):
    value = str(value).strip()
    if re.fullmatch(r"(?:UC|UU)[A-Za-z0-9_-]{22}", value):
        return "UC" + value[2:]
    if re.fullmatch(r"UUMO[A-Za-z0-9_-]{22}", value):
        return "UC" + value[4:]
    return value


def empty_channel(channel_id=""):
    return dict(id=channel_id, name="", **{key: False for key in CHANNEL_TABLES},
                **{key: "" for key in RULE_TABLES})


def channels_from_config(doc):
    channels = {}
    for mode, table in CHANNEL_TABLES.items():
        for name, raw_id in doc.get(table, {}).items():
            channel_id = canonical_id(raw_id)
            channel = channels.setdefault(channel_id, empty_channel(channel_id))
            if not channel["name"]:
                channel["name"] = name
            channel[mode] = True
    for field, table in RULE_TABLES.items():
        for raw_id, value in doc.get(table, {}).items():
            channel_id = canonical_id(raw_id)
            channel = channels.setdefault(channel_id, empty_channel(channel_id))
            if not channel[field] or raw_id == channel_id:
                channel[field] = str(value)
    for channel in channels.values():
        channel["name"] = channel["name"] or channel["id"]
    return sorted(channels.values(), key=lambda channel: channel["name"].casefold())


def validate_channel(data):
    channel_id = canonical_id(data.get("id", ""))
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("Enter a valid channel ID, or use Look up to find it from a channel link or @handle.")
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 200:
        raise ValueError("Enter a channel name between 1 and 200 characters.")
    channel = empty_channel(channel_id)
    channel["name"] = name
    for mode in CHANNEL_TABLES:
        channel[mode] = bool(data.get(mode))
    if not any(channel[mode] for mode in CHANNEL_TABLES):
        raise ValueError("Choose at least one archive option for this channel.")
    for field in RULE_TABLES:
        value = str(data.get(field, "")).strip()
        if len(value) > 4000:
            raise ValueError("Filters and output templates must be 4,000 characters or fewer.")
        if value and field.endswith("regex"):
            try:
                re.compile(value)
            except re.error as error:
                label = "Title" if field == "title_regex" else "Description"
                raise ValueError(f"{label} filter is not a valid regular expression: {error}") from error
        channel[field] = value
    return channel


def remove_channel(doc, channel_id):
    for table in CHANNEL_TABLES.values():
        for name, raw_id in list(doc.get(table, {}).items()):
            if canonical_id(raw_id) == channel_id:
                del doc[table][name]
    for table in RULE_TABLES.values():
        for raw_id in list(doc.get(table, {})):
            if canonical_id(raw_id) == channel_id:
                del doc[table][raw_id]


def save_channel(path, data, expected_revision, editing=False):
    channel = validate_channel(data)
    with CONFIG_LOCK:
        doc, current_revision = read_config(path)
        if current_revision != expected_revision:
            raise ConfigConflict("Configuration changed in another window. Reload this page before saving again.")
        existing = {item["id"]: item for item in channels_from_config(doc)}
        if editing and channel["id"] not in existing:
            raise ConfigConflict("This channel has been removed. Reload the channel list.")
        if not editing and channel["id"] in existing:
            raise ValueError("This channel is already added. Edit its existing entry instead.")
        original_ids = {}
        for mode, table in CHANNEL_TABLES.items():
            for name, raw_id in doc.get(table, {}).items():
                if canonical_id(raw_id) == channel["id"]:
                    original_ids.setdefault(mode, raw_id)
                elif name.casefold() == channel["name"].casefold():
                    raise ValueError("That name belongs to another channel. Choose a different display name.")
        remove_channel(doc, channel["id"])
        for mode, table in CHANNEL_TABLES.items():
            if channel[mode]:
                if table not in doc:
                    doc[table] = tomlkit.table()
                doc[table][channel["name"]] = original_ids.get(mode, channel["id"])
        for field, table in RULE_TABLES.items():
            if channel[field]:
                if table not in doc:
                    doc[table] = tomlkit.table()
                key = SingleKey(channel["id"], KeyType.Basic)
                doc[table].add(key, channel[field])
        write_config(path, tomlkit.dumps(doc), expected_revision)
    return channel


def delete_channel(path, channel_id, expected_revision):
    with CONFIG_LOCK:
        doc, _ = read_config(path)
        if channel_id not in {item["id"] for item in channels_from_config(doc)}:
            raise ValueError("This channel is no longer in your configuration.")
        remove_channel(doc, channel_id)
        write_config(path, tomlkit.dumps(doc), expected_revision)


def channel_url(value):
    value = value.strip()
    if len(value) > 500:
        raise ValueError("Enter a YouTube channel link, @handle, or channel ID.")
    normalized = canonical_id(value)
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", normalized):
        return f"https://www.youtube.com/channel/{normalized}"
    if value.startswith("@") and re.fullmatch(r"@[^\s/?#]+", value):
        return f"https://www.youtube.com/{value}"
    parsed = urlparse(value if "://" in value else "https://" + value)
    if (parsed.scheme not in {"https", "http"}
            or parsed.netloc.lower() not in {"youtube.com", "www.youtube.com", "m.youtube.com"}):
        raise ValueError("Use a youtube.com channel link, @handle, or channel ID.")
    parts = parsed.path.strip("/").split("/")
    if parts[0].startswith("@") and len(parts[0]) > 1:
        path = parts[0]
    elif len(parts) >= 2 and parts[0] in {"channel", "c", "user"} and parts[1]:
        path = "/".join(parts[:2])
    else:
        raise ValueError("Use a channel link rather than a video or playlist link.")
    return "https://www.youtube.com/" + path
