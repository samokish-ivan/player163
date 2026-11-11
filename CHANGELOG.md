# Changelog

All notable changes to player163. Newest first.

## [1.6.0] — 2026-11-11

### Added
- **Artist pictures** in the Artists list, as round avatars:
  - **Your own picture**: right-click an artist → *Set Artist Picture…*. It always takes priority, and stays with the artist when you rename them.
  - **Free photos from Wikimedia Commons** (optional): the first time you open Artists, player163 asks whether to look them up. If you agree, it matches each artist on MusicBrainz and Wikidata and downloads a freely licensed photo. Hover a photo to see the photographer and license; right-click → *About This Photo* opens its Commons page, or *Remove This Photo* if it's the wrong one. Turn it on or off anytime in the ☰ menu.
  - Artists without a picture show a clean initial instead.

### Changed
- The Artists list no longer shows album covers next to artist names.

## [1.5.0] — 2026-11-11

### Added
- **Album art everywhere**: a small cover next to each album name in every song list, and each artist in the Artists list shows the cover of their newest album.
- **Album grid**: in Albums, switch between the list and a big cover-art grid (buttons next to rescan). Click a cover to open the album; right-click to play it, queue it, rename it or edit its tags.
- **Play Next / Add to Queue**: right-click songs, an artist or an album. Queued songs play before the rest and show their place (1, 2, 3…) in the list. *Clear Up Next* is in the right-click menu and the ☰ menu.
- **Volume leveling** (☰ → Volume Leveling → Off / Per Song / Per Album; Per Album is the default): evens out loud and quiet songs. Uses the ReplayGain values already in your files, and quietly measures songs that don't have them in the background (once, then remembered). Never boosts a song past the point where it would clip.
- **Equalizer** (☰ → Equalizer…, Ctrl+Shift+E): 10 bands with presets: Flat, Bass Boost, Treble Boost, Vocal, Rock, Pop, Jazz, Classical, Electronic, Late Night.
- **New music shows up by itself**: songs you add, move or delete in your music folder appear in (or leave) the library within a few seconds, with no need to press F5.

### Changed
- Rounder, roomier look: all buttons are rounded whatever your system theme, and Songs / Artists / Albums / Favorites are separate spaced-out buttons.

### Fixed
- Playback (for example in shuffle) stopped when the next song had been moved, renamed or deleted since the last scan. Missing songs are now skipped, with a note saying which.

## [1.4.0] — 2026-11-11

### Added
- **Album artwork in the Albums list**: each album shows a small cover thumbnail (from the songs' embedded art or a cover.jpg in the folder). Albums without art get a subtle placeholder. Thumbnails load in the background and are cached, so large libraries stay fast.

### Changed
- Remembers more between sessions: the window's position, and the artist or album you had selected in each view. On startup the list scrolls to the song you were last playing.
- A remembered window position on a monitor that's no longer connected is ignored, so the window never opens off-screen.

### Fixed
- Clicking "All Artists" or "All Albums" after selecting an artist or album showed an empty list instead of every song.

## [1.3.0] — 2026-11-11

### Added
- **Automatic updates**: installing the .deb adds the player163 update source, so new versions show up in Mint's Update Manager. Updates are signed; tampered or unsigned updates are refused.
- Turn updates off anytime in Software Sources; reinstalling the app respects that choice. Uninstalling removes the source.
- Source code, releases and the download page are on GitHub: https://github.com/samokish-ivan/player163

## [1.2.1] — 2026-11-11

### Security
- Oversized or booby-trapped album covers ("decompression bombs") are refused before decoding, instead of using gigabytes of memory or crashing the app.
- Cover image files larger than 30 MB are skipped.
- Exported .m3u playlists can't be altered by line breaks hidden in song tags or filenames.

## [1.2.0] — 2026-11-11

### Added
- **Favorites 👍**: thumbs-up button in the player bar and the mini player (Ctrl+D), a 👍 column in every list, and *Add/Remove from Favorites* on the right-click menu.
- **Favorites tab** (Ctrl+4): your favorites as a playlist, newest first, with shuffle and repeat.
- **Export Favorites as Playlist…** (.m3u) in the ☰ menu.
- Favorites are kept when a file is renamed or moved (matched by title, artist and album on rescan).

### Changed
- **Volume is linked to Mint's sound settings**: the slider is player163's own slider in *Sound Settings → Applications*. Changes there show up in the app (main and mini), and mute stays in sync. Other apps and the overall system volume are unaffected.
- Author set to Ivan Samokish, with a link to ivansamokish.com (About dialog and package homepage).

## [1.1.0] — 2026-11-11

### Added
- **Mini player**: compact strip with album art, song info, play controls, shuffle/repeat, volume and seek bar. Toggle with Ctrl+M, the button in the player bar, or ☰ → *Mini Player*.
- Drag the mini player anywhere; it remembers its position.
- Right-click the mini player for *Keep on Top*.
- The app reopens in mini mode if you quit while in it.
- Rounded album art in the player bar and mini player.

## [1.0.0] — 2026-11-11

### Added
- First release.
- Pick a music folder once; fast rescans only read new or changed files.
- Songs, Artists and Albums views, type-anywhere search, sortable columns.
- Tag editing written to the files: single song, many songs at once, or *Rename Artist / Album* to fix typos and merge duplicates.
- Gapless playback via GStreamer: FLAC, ALAC, MP3, AAC, Ogg, Opus, WAV, AIFF, APE, WavPack, WMA and more.
- Shuffle, repeat (off / all / one), volume and mute, seek bar; remembers the last song and position.
- Media keys and Cinnamon sound applet support (MPRIS).
- player163 branding: black/yellow dark theme and a light theme (☰ menu), vector app icon and wordmark.
- Opens audio files from the file manager (single instance).
