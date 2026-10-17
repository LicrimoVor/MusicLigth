"""Rank recordings by identity first, then by available source audio quality."""
from difflib import SequenceMatcher
import re
from urllib.parse import parse_qs, urlparse, urlunparse

from music.playlist_import import normalized


VERSIONS = (
    r"\b(?:remix|ремикс|rmx)\b", r"\b(?:live|concert|лайв|концерт|ao vivo)\b",
    r"\b(?:cover|кавер|перепел)\b", r"\b(?:karaoke|караоке)\b", r"\b(?:instrumental|инструментал)\b",
    r"\b(?:slowed|slow reverb|замедлен\w*)\b", r"\b(?:sped up|speed up|speedup|nightcore|ускорен\w*)\b",
    r"\b(?:bass ?boosted|bassboost|бассбуст|8d|8 д)\b", r"\b(?:reverb|реверб)\b",
    r"\b(?:extended|расширенн\w*)\b", r"\b(?:radio edit|radio version)\b",
    r"\b(?:acoustic|акустик\w*)\b", r"\b(?:demo|демо)\b", r"\b(?:a ?capp?ella|акапелла)\b",
    r"\b(?:bootleg|бутлег)\b", r"\b(?:mashup|мэшап)\b", r"\b(?:edit|эдит)\b",
)
NOISE = re.compile(r"\b(?:official|audio|video|lyrics?|lyric video|visuali[sz]er|hd|hq|4k|1080p|music video|официальн\w*|клип)\b")


def recording_url(entry):
    value = entry.get("webpage_url") or entry.get("url") or ""
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in ("youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"):
        identity = parsed.path.strip("/") if host == "youtu.be" else (parse_qs(parsed.query).get("v") or [""])[0]
        if identity:
            return "https://www.youtube.com/watch?v=" + identity
    scheme = "https" if parsed.scheme == "http" and host in ("soundcloud.com", "www.soundcloud.com", "api.soundcloud.com") else parsed.scheme
    return urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def expected_duration(track):
    value = track.get("expected_duration") or (0 if track.get("search_by_name") else track.get("duration")) or 0
    return float(value)


def _without_artist(value, artist):
    if artist and value.startswith(artist + " "):
        return value[len(artist):].strip()
    return value


def _clean_requested_title(core):
    core = re.sub(r"\bofficial (?:lyric video|music video|audio|video)\b", " ", core)
    semantic = {"audio", "video"} & set(core.split())
    return " ".join(NOISE.sub(lambda match: match[0] if match[0] in semantic else " ", core).split()) or core.strip()


def search_query(track):
    if track.get("search_query") and not track.get("search_by_name"):
        return track["search_query"]
    artist = normalized(track.get("artist") or "")
    core = _without_artist(normalized(track.get("original_title", track["title"])), artist)
    title = _clean_requested_title(core)
    return " ".join(value for value in (artist, title) if value)


def match_score(entry, track):
    title = normalized(track.get("original_title", track["title"]))
    artist = normalized(track.get("artist") or "")
    full_title = normalized(entry.get("title") or "")
    requested_version = any(re.search(version, title) for version in VERSIONS)
    label = full_title if requested_version else normalized(entry.get("track") or entry.get("title") or "")
    channel = normalized(" ".join(str(entry.get(key) or "") for key in ("artist", "creator", "channel", "uploader")))
    if not label or entry.get("is_live") or entry.get("live_status") in ("is_live", "is_upcoming"):
        return None
    if entry.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
        return None
    requested = title + " " + artist
    for version in VERSIONS:
        if bool(re.search(version, requested)) != bool(re.search(version, full_title + " " + label)):
            return None
    for unwanted in ("full album", "compilation", "playlist", "dj set", "full concert", "1 hour", "1 час", "podcast"):
        if unwanted in label and unwanted not in requested:
            return None
    wanted_core = _without_artist(title, artist)
    wanted = _clean_requested_title(wanted_core)
    protected = set(wanted.split())
    candidate = " ".join(NOISE.sub(lambda match: match[0] if match[0] in protected else " ", _without_artist(label, artist)).split())
    if not wanted or not candidate:
        return None
    wanted_words, found_words = set(wanted.split()), set(candidate.split())
    recall = len(wanted_words & found_words) / len(wanted_words)
    precision = len(wanted_words & found_words) / max(1, len(found_words))
    similarity = max(SequenceMatcher(None, wanted, candidate).ratio(), 2 * precision * recall / max(1e-9, precision + recall))
    if recall < 0.75 or similarity < 0.68:
        return None
    artist_words = set(artist.split()) - {"feat", "ft", "and", "the", "official"}
    artist_score = len(artist_words & set((label + " " + channel).split())) / max(1, len(artist_words)) if artist_words else 1.0
    if artist_words and artist_score < 0.5:
        return None
    wanted_seconds, seconds = expected_duration(track), float(entry.get("duration") or 0)
    # A copied VK row can report a preview's duration; it must not force a clip.
    compare_duration = wanted_seconds >= 60 or 0 < wanted_seconds < 30
    difference = abs(seconds - wanted_seconds) if compare_duration and seconds else 0
    if compare_duration and seconds and difference > max(8, wanted_seconds * 0.08):
        return None
    if seconds and seconds < 60 and not (0 < wanted_seconds < 60):
        return None
    duration_score = max(0, 1 - difference / max(5, wanted_seconds * 0.06)) if seconds else 0.4
    official = 0.07 if "topic" in channel else 0
    official += 0.07 if entry.get("channel_is_verified") else 0
    official += 0.08 if "official audio" in normalized(entry.get("title") or "") else 0
    return 0.65 * similarity + 0.23 * artist_score + 0.12 * duration_score + official


def rank_results(entries, track):
    excluded = {recording_url({"url": url}) for url in track.get("excluded_urls", [])}
    found = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = recording_url(entry)
        if not url.startswith("https://") or url in excluded:
            continue
        score = match_score(entry, track)
        if score is not None and (url not in found or found[url]["match_score"] < score):
            found[url] = entry | {"webpage_url": url, "match_score": score}
    return sorted(found.values(), key=lambda item: (-item["match_score"], item["webpage_url"]))


def choose_result(entries, track):
    ranked = rank_results(entries, track)
    if not ranked:
        raise ValueError("Не найден подходящий трек. Уточните название или добавьте локальный файл.")
    return ranked[0]


def audio_format(info):
    available = []
    for item in info.get("formats") or [info]:
        if not isinstance(item, dict):
            continue
        codec = str(item.get("acodec") or "").lower()
        if not codec or codec == "none" or item.get("vcodec") not in (None, "none") or item.get("has_drm"):
            continue
        if re.search(r"preview|snippet|sample", str(item.get("format_id", "")) + " " + str(item.get("format_note", "")), re.I):
            continue
        rate = float(item.get("asr") or 0)
        if rate and rate < 32000:
            continue
        bitrate = float(item.get("abr") or 0)
        if not bitrate and item.get("filesize") and info.get("duration"):
            bitrate = float(item["filesize"]) * 8 / float(info["duration"]) / 1000
        lossless = any(value in codec for value in ("flac", "alac", "pcm", "wavpack"))
        opus = "opus" in codec
        if not lossless and bitrate and bitrate < (96 if opus else 128):
            continue
        effective = bitrate * (1.35 if opus else 1)
        quality = 1.1 if lossless else min(1.0, effective / 320) if bitrate else 0.25
        if item.get("audio_channels") == 1:
            quality *= 0.9
        if item.get("format_id"):
            available.append((quality, bitrate, item | {"abr": bitrate, "quality_score": quality}))
    if not available:
        raise ValueError("У источника нет полноценного аудио приемлемого качества (только превью или низкий битрейт).")
    return max(available, key=lambda item: item[:2])[2]


def source_name(url):
    host = (urlparse(url).hostname or "").lower()
    return "SoundCloud" if host.endswith("soundcloud.com") else "YouTube" if "youtu" in host else host


def source_details(track):
    bits = [track.get("resolved_source") or "", track.get("source_codec") or ""]
    if track.get("source_bitrate_kbps"):
        bits.append(f"{track['source_bitrate_kbps']:.0f} кбит/с")
    return " · ".join(value for value in bits if value)
