# Releasing player163

Every published GitHub Release builds the .deb, attaches it to the release, and updates the
signed update source at **https://samokish-ivan.github.io/player163/**. Anyone who installed
player163 1.3.0 or newer then gets the new version in Mint's **Update Manager**.

---

## One-time setup (about 10 minutes)

### 1. Create the repository and push the code

On github.com: **+ → New repository** → name `player163`, **Public**, leave "Add a README"
unticked → **Create repository**. Then, in a terminal in this folder:

```bash
git remote add origin https://github.com/samokish-ivan/player163.git
git push -u origin main
```

### 2. Create the signing key and give it to GitHub

The key proves updates really come from you. On your own computer:

```bash
gpg --batch --passphrase '' --quick-gen-key "player163 updates <ivan.samokish@gmail.com>" rsa4096 sign never
gpg --armor --export-secret-keys "player163 updates" > player163-signing-key.asc
```

On GitHub: repository **Settings → Secrets and variables → Actions → New repository secret**

- Name: `APT_SIGNING_KEY`
- Secret: open `player163-signing-key.asc` in a text editor and paste **everything**, including
  the `-----BEGIN` and `-----END` lines.

Then **back up `player163-signing-key.asc`** somewhere safe and offline (a password manager or
a USB stick) and delete the copy on your desktop.

> ⚠️ Never commit this file or share it. If you lose it, existing users can't receive updates
> until they install a new .deb by hand. If it leaks, someone could sign fake updates.

### 3. Publish the first release

**Releases → Draft a new release**

- **Choose a tag:** type `v1.3.0` → *Create new tag*
- **Title:** `player163 1.3.0`
- **Description:** paste the 1.3.0 section from `CHANGELOG.md`
- **Publish release**

Watch the **Actions** tab. After about a minute, the release has two files attached:
`player163_1.3.0_all.deb` and `player163_latest_all.deb`.

### 4. Turn on GitHub Pages (only once, after step 3)

**Settings → Pages → Build and deployment**

- **Source:** Deploy from a branch
- **Branch:** `gh-pages`, folder `/ (root)` → **Save**

After a minute or two, https://samokish-ivan.github.io/player163/ shows the download page.

---

## Every new version

1. Add a section to the top of `CHANGELOG.md` (same style as the others) and set
   `VERSION = "x.y.z"` near the top of `src/player163.py`. Commit and push.
2. **Releases → Draft a new release** → tag `vx.y.z` → paste the changelog section →
   **Publish release**.

That's it. Within a day (Update Manager's next refresh), users see the update.

**Version numbers must go up:** 1.3.0 → 1.3.1 → 1.4.0. A release with a lower or repeated
version won't be offered as an update.

---

## Good to know

- **Download link for your site and posts** (always the newest version):
  https://github.com/samokish-ivan/player163/releases/latest/download/player163_latest_all.deb
- **Users of 1.2.1 or earlier** have no update source yet. They install 1.3.0 (or newer) by
  hand once, and from then on updates are automatic.
- **Users can turn updates off** in *Software Sources → Additional repositories*. The app
  respects that, even when they reinstall it later. Uninstalling also removes the source.
- **If a release fails** (red ✗ in Actions): open it to see the error. The most common causes
  are a missing `APT_SIGNING_KEY` secret or a tag that isn't `v` + numbers. Fix it, then
  **Actions → Release → Run workflow** to republish the update site, or delete the release
  and publish it again.
- **Building by hand:** `./build-deb.sh` makes a local .deb without the update source.
  `VERSION=1.3.1 KEYRING=key.gpg ./build-deb.sh` makes one with it.
