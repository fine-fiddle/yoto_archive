# Yoto Archive

A small Python CLI to back up the cover, track icons, and audio from a normal
Yoto card's NFC URL. It uses only Python's standard library: no browser,
JavaScript interpreter, Yoto login, or third-party runtime dependencies.

Requires Python 3.10 or newer.

## Back up a card

Read the card's full URL with an NFC reader, including the query string after
`?`. Quote the URL so your shell preserves it:

```sh
python3 yoto_archive.py 'https://yoto.io/CARD_ID?KEY=VALUE'
```

The URL may also be the redirected `https://share.yoto.co/p/...` address.
Downloads go into `archives/<card title>/`. To choose another parent directory:

```sh
python3 yoto_archive.py 'https://yoto.io/CARD_ID?KEY=VALUE' --output /path/to/backups
```

Optionally install the command in a virtual environment with
`python3 -m pip install .`, then run `yoto-archive URL`.

## Archive contents

```text
archives/<card title>/
  cover.png
  tracks/
    001 - First track.m4a
    002 - Second track.m4a
  icons/
    001 - First track.png
    002 - Second track.png
  manifest.json
```

Extensions follow the response's content type. In particular, Yoto's `aac`
tracks served as MP4 audio are saved as `.m4a`, without conversion. Track order
is preserved across chapters. Unsafe filename characters are replaced, while
apostrophes and Unicode titles are retained.

The manifest records card metadata, chapter/track order, local asset paths,
file sizes, and SHA-256 checksums. It omits the NFC URL, signed download URLs,
and user identifiers. This provides useful metadata for the planned restore
feature; restoration itself is not implemented yet.

Downloads stream to a temporary directory. The final archive appears only
after every asset has downloaded and passed size/checksum checks when supplied
by Yoto. Failures remove the temporary files and exit with a readable error;
rerun the command to fetch fresh signed URLs. An existing archive is never
overwritten: choose another `--output` directory for a second backup. Network
operations have a 30-second timeout, adjustable with `--timeout SECONDS`.

This implements [issue #1: Backup normal card content](https://github.com/fine-fiddle/yoto_archive/issues/1).
Virtual cards and restoring archives are separate planned features. The page
must expose downloadable audio; previews and streaming-only tracks are not
substituted for the card's content.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic card pages and responses and require no network access.
The embedded page data structure was investigated using the browser tool
linked in the issue, [Yoto Archival Downloader](https://github.com/yack-order/yoto-archival-downloader).
