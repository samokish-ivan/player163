# player163

A small, no-fuss music player for Linux Mint (and Ubuntu / Debian).

## Install

**[Download the latest player163 (.deb)](https://github.com/samokish-ivan/player163/releases/latest/download/player163_latest_all.deb)**,
double-click it and choose **Install Package**. Or in a terminal:

    sudo apt install ./player163_latest_all.deb

Then start **player163** from the menu (Sound & Video), or run `player163`.

Requirements: Linux Mint 21 or newer, Ubuntu 22.04 or newer, or Debian 12 or newer. Everything
else (Python 3, GTK 3, GStreamer, mutagen) is installed automatically.

Uninstall: `sudo apt remove player163`

## Updates

Installing the .deb also adds the player163 update source
(https://samokish-ivan.github.io/player163), so new versions appear in Mint's **Update Manager**
like any other update. To turn this off, disable the source in
*Software Sources → Additional repositories*; reinstalling player163 keeps that choice.

## Use

1. Click **Choose Music Folder…** and pick where your music lives. It scans it once;
   after that, starting the app or pressing **F5** only re-reads new or changed files.
2. Switch between **Songs / Artists / Albums** at the top. Just start typing to search.
3. Double-click (or Enter) to play. The queue is whatever list you're looking at.

### Fixing tags (typos etc.)

- **Misspelled artist:** Artists view → right-click the bad name → *Rename Artist…* →
  type (or pick) the right name. All its songs are updated and merge into the correct artist.
- **Album name:** Albums view → right-click → *Rename Album…*
- **Anything else:** select one or many songs → right-click → *Edit Tags…* (or Ctrl+E / F2).
  With many songs selected, only the fields you change are written.

Tags are written to the files themselves, so other players see the fixes too.

## Favorites 👍

- Click the 👍 next to the song title (main window or mini player), or press **Ctrl+D**,
  to favorite the playing song. Click 👍 again to remove it.
- In any list, click the 👍 column to favorite a song, or right-click a selection →
  *Add to Favorites*.
- **Favorites** tab (Ctrl+4) is your playlist, newest first. Double-click to play it;
  shuffle/repeat work as usual.
- ☰ → *Export Favorites as Playlist…* saves an .m3u you can use in other players.
- Favorites survive rescans and are kept if you rename or move a file (matched by
  title, artist and album).

## Volume

The volume slider is player163's own slider in Mint's *Sound Settings → Applications*.
Moving one moves the other, and mute stays in sync. It doesn't change other apps;
your keyboard volume keys still control the overall system volume.

## Mini player

Click the mini-player button at the right end of the player bar (or press **Ctrl+M**) to shrink
to a tiny strip: album art, song, play controls, shuffle/repeat, volume and seek bar.

- Drag it anywhere by the art or background; it remembers where you put it.
- Back to the library: the ⤢ button, a double-click, or **Ctrl+M**.
- Right-click it for **Keep on Top**.
- If you quit while in mini mode, it opens in mini mode next time.

## Look

Black + yellow dark theme by default; switch to the light theme from the ☰ menu
(*Dark Theme* / *Light Theme*). Brand assets (icon + wordmark lockup SVGs) are in `data/`.

## Formats

Anything GStreamer can decode: FLAC, ALAC (.m4a), MP3, AAC, Ogg Vorbis, Opus, WAV, AIFF,
APE, WavPack, WMA, Musepack, DSD (.dsf) and more. Playback is gapless.
For the rarer formats, also install: `sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly`

## Keys

| Key | Action |
|---|---|
| Space | Play / pause |
| Ctrl+→ / Ctrl+← | Next / previous |
| Ctrl+↑ / Ctrl+↓ | Volume |
| Ctrl+S / Ctrl+R | Shuffle / repeat (off → all → one) |
| Ctrl+1 / 2 / 3 / 4 | Songs / Artists / Albums / Favorites |
| Esc | Clear search |
| Ctrl+E or F2 | Edit tags |
| Ctrl+D | Favorite the playing song |
| Ctrl+4 | Favorites |
| Ctrl+J | Jump to playing song |
| Ctrl+M | Mini player ⇄ library |
| Ctrl+O / F5 | Choose folder / rescan |

Keyboard media keys and the Cinnamon sound applet work too (MPRIS).

## Files

- Settings: `~/.config/player163/config.json`
- Library cache: `~/.local/share/player163/library.db` (safe to delete; it rebuilds)

## Rebuild the .deb

    ./build-deb.sh

See CHANGELOG.md for version history, and RELEASING.md for how releases are published.

By Ivan Samokish — https://ivansamokish.com

Built with Python, GTK 3, GStreamer and mutagen. MIT licence.
