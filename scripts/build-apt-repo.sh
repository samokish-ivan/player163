#!/bin/bash
# Builds a signed APT repository from a folder of player163 .deb files.
#
#   scripts/build-apt-repo.sh <debs-dir> <output-dir> <gpg-key-id> [repo-url]
#
# The output is a static site (works on GitHub Pages):
#   dists/stable/{InRelease,Release,Release.gpg,main/binary-*/Packages(.gz)}
#   pool/main/p/player163/*.deb
#   player163.asc (public key), index.html (install instructions)
# Set APT_SIGNING_PASSPHRASE if the signing key has a passphrase.
set -euo pipefail

DEBS="$1"
OUT="$2"
KEYID="$3"
REPO_URL="${4:-https://samokish-ivan.github.io/player163}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHES="amd64 arm64 armhf i386"

GPG=(gpg --batch --yes --local-user "$KEYID")
if [ -n "${APT_SIGNING_PASSPHRASE:-}" ]; then
  GPG+=(--pinentry-mode loopback --passphrase "$APT_SIGNING_PASSPHRASE")
fi

rm -rf "$OUT"
mkdir -p "$OUT/pool/main/p/player163"
shopt -s nullglob
debs=("$DEBS"/player163_[0-9]*_all.deb)
if [ ${#debs[@]} -eq 0 ]; then
  echo "No player163_<version>_all.deb files in $DEBS" >&2
  exit 1
fi
cp "${debs[@]}" "$OUT/pool/main/p/player163/"

cd "$OUT"
for arch in $ARCHES all; do
  dir="dists/stable/main/binary-$arch"
  mkdir -p "$dir"
  dpkg-scanpackages --multiversion pool/ /dev/null 2>/dev/null > "$dir/Packages"
  gzip -9nk "$dir/Packages"
done

apt-ftparchive \
  -o APT::FTPArchive::Release::Origin="player163" \
  -o APT::FTPArchive::Release::Label="player163" \
  -o APT::FTPArchive::Release::Suite="stable" \
  -o APT::FTPArchive::Release::Codename="stable" \
  -o APT::FTPArchive::Release::Architectures="$ARCHES all" \
  -o APT::FTPArchive::Release::Components="main" \
  -o APT::FTPArchive::Release::Description="player163 updates by Ivan Samokish" \
  release dists/stable > Release.tmp
mv Release.tmp dists/stable/Release

"${GPG[@]}" --armor --detach-sign --output dists/stable/Release.gpg dists/stable/Release
"${GPG[@]}" --clearsign --output dists/stable/InRelease dists/stable/Release

gpg --batch --armor --export "$KEYID" > player163.asc
touch .nojekyll

LATEST=$(ls pool/main/p/player163/ | sed -n 's/^player163_\(.*\)_all\.deb$/\1/p' | sort -V | tail -1)
sed -e "s#@REPO_URL@#${REPO_URL}#g" -e "s#@LATEST@#${LATEST}#g" "$HERE/repo-index.html" > index.html

echo "APT repository built in $OUT (latest: $LATEST, $(ls pool/main/p/player163 | wc -l) package(s))"
