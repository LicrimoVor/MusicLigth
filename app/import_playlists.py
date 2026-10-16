"""CLI: python app/import_playlists.py [data/*.txt] [--output data/playlists]."""
from music.playlist_import import main


if __name__ == "__main__":
    raise SystemExit(main())
