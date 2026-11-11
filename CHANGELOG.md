# Changelog

All notable changes to player163. Newest first.

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
