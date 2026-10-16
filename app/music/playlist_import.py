"""Parse copied VK playlist fragments without depending on generated CSS names."""
from html.parser import HTMLParser
import argparse
import glob
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"\w+", value))


def track_key(title, artist=""):
    return normalized(artist), normalized(title)


class PlaylistParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    FIELDS = {"MusicTrackRow_Title": "title", "MusicTrackRow_Authors": "artist",
              "MusicTrackRow_Duration": "duration"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.row = None
        self.row_depth = 0
        self.capture = []
        self.entries = []
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.VOID:
            return
        self.stack.append(tag)
        if attrs.get("data-audio-id") and self.row is None:
            self.row = {"source_id": attrs["data-audio-id"], "title": "", "artist": "", "duration": ""}
            self.row_depth = len(self.stack)
        if self.row is not None:
            field = self.FIELDS.get(attrs.get("data-testid"))
            if field:
                self.capture.append((field, len(self.stack)))
                if field == "title" and attrs.get("href", "").startswith("/audio"):
                    self.row["source_url"] = "https://vk.com" + attrs["href"]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.row is not None:
            for field, _ in self.capture:
                self.row[field] += data

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.row is not None and any(field == "artist" and level == depth for field, level in self.capture):
            self.row["artist"] += ", "
        self.capture = [(field, level) for field, level in self.capture if level < depth]
        if self.row is not None and depth <= self.row_depth:
            row = self.row
            self.row = None
            title = " ".join(row["title"].split())
            artist = " ".join(row["artist"].strip(", ").split())
            key = track_key(title, artist)
            if title and key not in self.seen:
                self.seen.add(key)
                parts = row["duration"].strip().split(":")
                duration = 0
                if parts and all(part.isdigit() for part in parts):
                    for part in parts:
                        duration = duration * 60 + int(part)
                self.entries.append(row | {"title": title, "artist": artist, "duration": duration})
        del self.stack[depth - 1:]


def parse_playlist(path, cancel=None):
    path = Path(path)
    parser = PlaylistParser()
    with path.open(encoding="utf-8-sig", errors="replace") as source:
        while chunk := source.read(65536):
            if cancel is not None and cancel.is_set():
                from music.storage import CancelledAnalysis
                raise CancelledAnalysis()
            parser.feed(chunk)
    parser.close()
    return {"version": 1, "source": str(path.resolve()), "name": path.stem, "entries": parser.entries}


def save_playlist(path, output=None, cancel=None):
    from music.storage import atomic_json
    document = parse_playlist(path, cancel)
    if not document["entries"]:
        raise ValueError(f"{Path(path).name}: в HTML не найдены музыкальные треки")
    output = Path(output) if output is not None else Path(path).parent / "playlists"
    identity = hashlib.sha256(document["source"].encode("utf-8")).hexdigest()[:12]
    destination = output / (Path(path).stem + "-" + identity + ".json")
    atomic_json(destination, document)
    return destination, document


def load_playlist(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("entries"), list):
        raise ValueError(f"{Path(path).name}: неверный формат списка")
    if not isinstance(document.get("name"), str) or not isinstance(document.get("source"), str):
        raise ValueError(f"{Path(path).name}: отсутствует название или источник")
    for entry in document["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("title"), str) or not entry["title"].strip():
            raise ValueError(f"{Path(path).name}: неверная запись трека")
        if not isinstance(entry.get("artist", ""), str) or not isinstance(entry.get("duration", 0), (int, float)):
            raise ValueError(f"{Path(path).name}: неверные данные трека")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description="Сохранить треки из скопированного HTML VK в JSON-плейлисты")
    parser.add_argument("paths", nargs="*", default=[str(Path(__file__).resolve().parents[1] / "data")],
                        help="HTML/TXT файлы, папки или маски *.txt")
    parser.add_argument("--output", type=Path, help="Папка JSON; по умолчанию playlists рядом с исходным файлом")
    args = parser.parse_args(argv)
    files = []
    for value in args.paths:
        for path in glob.glob(value):
            path = Path(path)
            files.extend(sorted(path.glob("*.txt")) if path.is_dir() else [path])
    if not files:
        parser.error("Не найдены файлы для импорта")
    failed = False
    for path in dict.fromkeys(files):
        try:
            destination, document = save_playlist(path, args.output)
            print(f"{path.name}: {len(document['entries'])} треков -> {destination}")
        except (OSError, ValueError) as exc:
            print(str(exc))
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
