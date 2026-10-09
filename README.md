# Yoto Archive

A small Python CLI to back up a Yoto card's cover, track icons, and audio.
Normal cards use their NFC/share page without a login. Virtual cards and
content linked to Make Your Own (MYO) cards use an account access token.
The downloader can also archive an entire account and update it incrementally.
It uses only Python's standard library, with no third-party runtime dependencies.

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

## Back up virtual or linked MYO content

Some MYO share pages contain no card data, even though the player can play the
linked content. Use an access token from the Yoto account that can play it:

```sh
python3 yoto_archive.py 'https://yoto.io/CARD_ID?KEY=VALUE' --token-file yoto_token.json
```

With a token, you can also supply just the card ID. The downloader queries
`https://api.yotoplay.com/card/<card ID>?playable=true&signingType=s3`, which
resolves the MYO card's linked content and returns signed audio URLs. It uses
the same archive format and download checks as normal cards. A player proxy
is not required. This account endpoint was verified with a linked virtual
copy of *A Christmas Carol*; it is used by Yoto's account services and may
change independently of this tool.

To obtain a token, open your browser's developer tools and select the Network
tab, then sign in at [Yoto Make Your Own](https://my.yotoplay.com/). Find the
successful `POST https://login.yotoplay.com/oauth/token` request and copy its
response's `access_token` to a local file. The file may contain the token
alone or JSON with an `access_token` field. An email/password credential file
is not a token file; Yoto's web client requires its browser login flow.

Keep the token private. On macOS/Linux, restrict its file permissions with
`chmod 600 yoto_token.json`. Files named `yoto_token*`, `yoto_account`, and
`account_info` are ignored by Git in this repository. Tokens expire; repeat
the login process when the API reports HTTP 401/403. The account needs library
and audio access to the content. The downloader sends the token only to the
account API, excludes it from redirects and media requests, and never saves
it in the archive.

## Back up an account

Use the same token file to archive the family library and the user's owned
MYO playlists:

```sh
python3 yoto_archive.py --account --token-file yoto_token.json
```

The default directory is `archives/<account identifier>/`. The identifier is
the token's account subject, with unsafe filename characters replaced. An
explicit `--output /path/to/account-backup` selects the account archive itself.
The archive records its account identity and refuses an update from a different
account. It also refuses a nonempty target without an account manifest.

To inspect the collection before downloading media:

```sh
python3 yoto_archive.py --account --list --token-file yoto_token.json
```

Discovery combines `/card/family/library` with `/content/mine`, deduplicating
card IDs. Owned MYO entries resolve through `/content/<ID>` (including unlinked
playlists); family-only entries use `/card/<ID>` to resolve linked cards.
The MYO listing endpoint alone only lists owned MYO content. Both library
and MYO access, plus access to playable audio, are needed. The family-library
endpoint is an account-service endpoint rather than a guaranteed public API;
its current response shape is covered by synthetic fixtures and has been
verified with a live account token. Explicit next-page URLs are followed
only within the originating API route. Unknown pagination, malformed entries,
or a failed list are reported as incomplete, with a nonzero exit status.

Account requests run sequentially with a one-second minimum interval, including
media requests. Use `--request-interval SECONDS` to adjust the interval.
Connection failures and HTTP 429/500/502/503/504 responses receive at most three
retries with exponential backoff and jitter; `Retry-After` is respected. Denied
authentication is not retried. A card access denial or exhausted HTTP 429 stops
further card requests in that run. OAuth login and token refresh are still manual.

Account archives contain `account.json`, plus per-card revisions under
`cards/<card ID>/revisions/<revision ID>/`. The account manifest points to each
card's latest successful revision. Card IDs distinguish identical titles and
keep renamed cards associated with the same record. Linked MYO records also
retain the resolved content ID.

Rerunning the command checks local SHA-256 hashes, reuses unchanged audio and
images, and repairs missing or corrupt files. Rotating download signatures do
not trigger downloads. Metadata edits and track reordering reuse files with
stable media identities. For URLs without a content hash or content-addressed
path, a changed remote version/update timestamp invalidates the cache. Reused
files are hard-linked when possible and copied otherwise.

Streams and radio/podcast content retain source URLs and available metadata,
cover art, and icons; their audio URLs are never opened. Podcasts also retain
their feed URL when supplied. Mixed playlists download their ordinary file
tracks and save streaming tracks as URLs. Temporary signing parameters are
removed from stored source URLs, so a source exposed only through an expiring
signed link may require resolving it again later.

Each successful card update is published independently, then the account
manifest is replaced atomically. Failed updates keep the previous successful
revision. Previous revisions and cards removed from the library are retained;
removed cards are marked only after complete enumeration. The summary reports
archived, unchanged, newly updated URL-only, unavailable, and failed cards.
Incomplete runs exit nonzero and can be resumed with the same command. A
`.account-lock` prevents overlapping writers; after a process is forcibly
killed, verify it is no longer running before removing a leftover lock.

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

The single-card manifest records card metadata, chapter/track order, local
asset paths, file sizes, and SHA-256 checksums. It omits NFC URLs, signed
download URLs, and user identifiers. Account archives additionally retain
their account identity, stable asset identities, and streaming source URLs.
This provides useful metadata for the planned restore feature; restoration
itself is not implemented yet.

Downloads stream to a temporary directory. The final archive appears only
after every asset has downloaded and passed size/checksum checks when supplied
by Yoto. Failures remove the temporary files and exit with a readable error;
rerun the command to fetch fresh signed URLs. An existing archive is never
overwritten: choose another `--output` directory for a second backup. Network
operations have a 30-second timeout, adjustable with `--timeout SECONDS`.

This implements [issue #1: Backup normal card content](https://github.com/robot-assisted-projects/yoto_archive/issues/1),
[issue #3: Back up a virtual card](https://github.com/robot-assisted-projects/yoto_archive/issues/3),
and the account-archive implementation for [issue #5](https://github.com/robot-assisted-projects/yoto_archive/issues/5).
Restoring archives remains a separate planned feature. Single-card mode needs
downloadable audio and does not substitute previews or streaming tracks.
Account mode preserves streaming sources as described above.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic card pages and API responses and require no network access.
Account tests cover repeat runs, signature rotation, renames, file repair,
mixed streams, partial discovery, interrupted updates, and paced retries.
Live validation also covered account enumeration and initial/repeat backups
of a MYO playlist, a purchased card, and a radio card. The repeat run made no
media requests.
The embedded page data structure was investigated using the browser tool
linked in the issue, [Yoto Archival Downloader](https://github.com/yack-order/yoto-archival-downloader).
