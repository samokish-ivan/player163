#!/bin/bash
# Builds build/player163_<version>_all.deb from this folder.
#
#   ./build-deb.sh                       local build (no automatic-update source)
#   VERSION=1.3.1 ./build-deb.sh         override the version (the release workflow uses the tag)
#   KEYRING=key.gpg ./build-deb.sh       include the update source, signed with this public keyring
#   REPO_URL=https://... ./build-deb.sh  where the APT repository lives (default: GitHub Pages)
set -euo pipefail
cd "$(dirname "$0")"
SRC_VERSION=$(grep -m1 '^VERSION = ' src/player163.py | cut -d'"' -f2)
VERSION="${VERSION:-$SRC_VERSION}"
REPO_URL="${REPO_URL:-https://samokish-ivan.github.io/player163}"
KEYRING="${KEYRING:-}"
PKG=build/player163_${VERSION}_all
rm -rf build && mkdir -p "$PKG/DEBIAN"

install -Dm755 src/player163.py               "$PKG/usr/share/player163/player163.py"
if [ "$VERSION" != "$SRC_VERSION" ]; then
  sed -i "s/^VERSION = .*/VERSION = \"$VERSION\"/" "$PKG/usr/share/player163/player163.py"
fi
install -Dm644 data/player163.desktop         "$PKG/usr/share/applications/player163.desktop"
install -Dm644 data/player163.svg             "$PKG/usr/share/icons/hicolor/scalable/apps/player163.svg"
for s in 48 128 256; do
  install -Dm644 data/player163-$s.png "$PKG/usr/share/icons/hicolor/${s}x${s}/apps/player163.png"
done
mkdir -p "$PKG/usr/bin"
cat > "$PKG/usr/bin/player163" <<'EOF'
#!/bin/sh
# Use the system Python that has GTK bindings (guards against a replaced /usr/bin/python3)
for py in /usr/bin/python3 /usr/bin/python3.[0-9]*; do
  case "$py" in *-config|*m) continue;; esac
  if [ -x "$py" ] && "$py" -c 'import gi' 2>/dev/null; then
    exec "$py" /usr/share/player163/player163.py "$@"
  fi
done
echo "player163: no Python with GTK bindings found (sudo apt install python3-gi)" >&2
exit 1
EOF
chmod 755 "$PKG/usr/bin/player163"
install -Dm644 data/player163-lockup.svg "$PKG/usr/share/player163/branding/player163-lockup.svg"
install -Dm644 data/player163.svg "$PKG/usr/share/player163/branding/player163-icon.svg"
install -Dm644 README.md "$PKG/usr/share/doc/player163/README.md"
install -Dm644 CHANGELOG.md "$PKG/usr/share/doc/player163/CHANGELOG.md"
install -Dm644 LICENSE "$PKG/usr/share/doc/player163/copyright"
# Debian-format changelog generated from CHANGELOG.md
python3 - "$PKG/usr/share/doc/player163/changelog.Debian" <<'PY'
import re, sys, email.utils, datetime
out, entries, cur = sys.argv[1], [], None
for line in open("CHANGELOG.md", encoding="utf-8"):
    m = re.match(r"## \[?([\d.]+)\]? — (\d{4}-\d{2}-\d{2})", line)
    if m:
        cur = (m.group(1), m.group(2), []); entries.append(cur)
    elif cur and line.startswith("- "):
        cur[2].append(line[2:].strip().replace("**", ""))
with open(out, "w", encoding="utf-8") as f:
    for ver, date, items in entries:
        d = datetime.datetime.strptime(date, "%Y-%m-%d").replace(hour=12, tzinfo=datetime.timezone(datetime.timedelta(hours=-7)))
        f.write("player163 (%s) stable; urgency=low\n\n" % ver)
        for it in items:
            f.write("  * %s\n" % it)
        f.write("\n -- Ivan Samokish <ivan.samokish@gmail.com>  %s\n\n" % email.utils.format_datetime(d))
PY
gzip -9n "$PKG/usr/share/doc/player163/changelog.Debian"

SIZE=$(du -sk "$PKG/usr" | cut -f1)
cat > "$PKG/DEBIAN/control" <<EOF
Package: player163
Version: ${VERSION}
Section: sound
Priority: optional
Architecture: all
Installed-Size: ${SIZE}
Depends: python3 (>= 3.8), python3-gi, python3-gi-cairo, python3-mutagen, gir1.2-gtk-3.0, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-libav
Recommends: gstreamer1.0-plugins-ugly, gstreamer1.0-plugins-bad, gstreamer1.0-pulseaudio | gstreamer1.0-pipewire
Maintainer: Ivan Samokish <ivan.samokish@gmail.com>
Homepage: https://ivansamokish.com
Description: Small, no-fuss music player for your music folder
 Point it at a folder: it scans it, sorts by artist and album, and lets you
 fix tags (rename a misspelled artist across all its songs in one go).
 Gapless playback of FLAC, ALAC, MP3, AAC, Ogg, Opus, WAV, AIFF, APE,
 WavPack, WMA and more via GStreamer. Shuffle, repeat, volume, media keys.
EOF

# Automatic updates: ship the signing key and the scripts that add/remove the APT source
if [ -n "$KEYRING" ]; then
  install -Dm644 "$KEYRING" "$PKG/usr/share/keyrings/player163-archive-keyring.gpg"
  sed "s#@REPO_URL@#${REPO_URL}#" packaging/postinst > "$PKG/DEBIAN/postinst"
  install -m755 packaging/postrm "$PKG/DEBIAN/postrm"
  chmod 755 "$PKG/DEBIAN/postinst"
else
  echo "Note: no KEYRING given, so this build has no automatic-update source."
fi

find "$PKG" -type d -exec chmod 755 {} +
dpkg-deb --root-owner-group --build "$PKG" >/dev/null
echo "Built: build/player163_${VERSION}_all.deb"
