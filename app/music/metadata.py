from pathlib import Path

import mutagen


def read_metadata(path):
    result = {"title": Path(path).stem, "artist": "", "album": "", "title_from_tags": False}
    cover = None
    try:
        easy = mutagen.File(str(path), easy=True)
        if easy and easy.tags:
            for name in ("title", "artist", "album"):
                values = easy.tags.get(name)
                if values:
                    result[name] = str(values[0])
                    if name == "title":
                        result["title_from_tags"] = True
        media = mutagen.File(str(path))
        if media and media.tags:
            # WAV/AIFF use ID3 tags even when easy=True.
            for name, key in (("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB")):
                value = media.tags.get(key)
                if value and getattr(value, "text", None):
                    result[name] = str(value.text[0])
                    if name == "title":
                        result["title_from_tags"] = True
            if hasattr(media.tags, "getall"):
                pictures = media.tags.getall("APIC")
                if pictures:
                    cover = pictures[0].data
            if "covr" in media.tags:
                cover = bytes(media.tags["covr"][0])
        if getattr(media, "pictures", None):
            cover = media.pictures[0].data
        if cover is not None and len(cover) > 10 * 1024 * 1024:
            cover = None
    except (mutagen.MutagenError, OSError, ValueError, TypeError):
        pass
    return result, cover
