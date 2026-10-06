# PhotoScribe

AI-powered photo metadata generator that runs entirely on your Mac or Windows PC. Drop in your photos, generate title, caption, and keywords using a local AI model, review everything, then write it straight to your files as IPTC and XMP metadata.

**No cloud. No subscription. No data leaves your machine.**

![PhotoScribe](screenshot.png)

---

## What you need

PhotoScribe requires two free pieces of software in addition to the app itself.

### 1. PhotoScribe

Grab the latest build from the **[releases page](https://github.com/repomonkey/PhotoScribe/releases/latest)**.

**macOS:** Download `PhotoScribe.dmg`, open it, and drag PhotoScribe to your Applications folder. That's it — no Python, no Terminal, no setup.

> **First launch only:** macOS will ask if you're sure you want to open it. Click Open. This is normal for any app downloaded outside the Mac App Store and won't happen again.

**Windows:** Download `PhotoScribe-Setup.exe`, run it, and follow the installer, then launch PhotoScribe from the Start menu. ExifTool is bundled in, so there's nothing else to install for writing metadata.

### 2. An AI backend — LM Studio or Ollama

PhotoScribe needs a local AI backend running on your computer. This is what actually looks at your photos and generates the descriptions and keywords. Two options are supported, both available for Mac and Windows:

---

**Option A: LM Studio** *(recommended — no Terminal required)*

[LM Studio](https://lmstudio.ai) is a polished app (Mac and Windows) for running local AI models. Download it from **[lmstudio.ai](https://lmstudio.ai)**, install it, then:

1. Open LM Studio and go to the **Discover** tab
2. Search for **gemma-4** and download a model: `gemma-4-12b` for most machines, `gemma-4-e4b` for lighter ones. On a Mac, choose an MLX build
3. Go to the **Local Server** tab (the `<->` icon on the left) and click **Start Server**

In PhotoScribe's Settings tab, change the URL to `http://localhost:1234` and click Refresh.

---

**Option B: Ollama** *(lightweight, runs in the menu bar)*

Download from **[ollama.com/download](https://ollama.com/download)**. Once installed, open Terminal and pull a model:

```
ollama pull gemma4:12b-it-qat    # most machines, ~7.2GB
ollama pull gemma4:e4b-it-qat    # lighter, ~6.1GB
```

Ollama uses `http://localhost:11434` (the PhotoScribe default). It starts automatically at login.

---

### Choosing a model matters more than any other setting

The model you run decides how fast PhotoScribe is and how good its captions are, far more than anything you set inside the app. Choose badly and a folder that should take two minutes takes an hour.

Being able to read images is not enough to make a model a good fit. Plenty of models accept photos, and LM Studio and Ollama will happily load one that's far too big for your computer. It will still work, which is what makes this easy to miss. It will just be very slow, because a model that doesn't fit in memory gets read back from disk while it writes, again and again. Measured on a 32GB M2 Max MacBook Pro with the same photos:

| Model | Size on disk | Speed | Time per photo |
|---|---|---|---|
| Gemma 4 12B | 6.3GB | 33 tokens a second | about 8s |
| Gemma 4 26B-A4B | 15GB | 5–6 tokens a second | 20–30s |
| Qwen 3.8 27B | 15GB | 3–6 tokens a second, after starting near 17 | 20–70s |

The two larger models didn't write noticeably better captions for this job in testing. They were just three to eight times slower. Pick the model that fits with room to spare, not the biggest one that loads.

As a guide:

| Your computer | Model |
|---|---|
| 8GB Mac | Gemma 4 E2B |
| 16GB Mac, or a graphics card with 8GB | Gemma 4 E4B |
| 24–32GB Mac, or a 12–16GB graphics card | Gemma 4 12B |
| 48GB or larger Mac, or a 24GB graphics card | Gemma 4 26B-A4B |

A Mac shares its memory between the model, the system and everything else you have open, Lightroom and your browser included, so it needs more headroom than the model's size suggests. On a Mac, choose an MLX build in LM Studio.

**Not sure which to pick?** Click **Recommend Model** (top-right of Settings). PhotoScribe detects your graphics memory and RAM and suggests the model that fits, with the exact name to search for in LM Studio, or a one-click pull for Ollama.

![Model recommendation](screenshot-recommend.png)

#### Running slowly? It's almost certainly the model

A model that fits your computer writes at 15 tokens a second or more. One that doesn't falls to single figures, and nothing errors to tell you why. PhotoScribe watches for this and writes a warning to the **Log** tab when generation drops below 8 tokens a second. If you see it, or PhotoScribe just feels slow, switch to the model Recommend Model suggests before changing anything else.


### 3. ExifTool — writes metadata to your files

ExifTool is a small, free utility that does the actual work of embedding metadata into your photo files. PhotoScribe uses it behind the scenes when you click "Write Metadata to Files."

**Windows:** Nothing to do — ExifTool is bundled inside the installer.

**macOS:** ExifTool isn't bundled, so install it once. If it's missing, PhotoScribe will tell you and offer a direct link — no Terminal required, the ExifTool website provides a standard macOS `.pkg` installer:

👉 **[Download ExifTool for macOS](https://exiftool.org/install.html)**

Download the macOS Package, open it, and follow the prompts. Done.

You can generate and export metadata without ExifTool, but you won't be able to write it directly to files.

---

## How to use PhotoScribe

### The basics

1. **Drop your photos** into the left panel, or use the Browse buttons
2. **Fill in the batch context** — location, event, date, photographer. The more context you give the AI, the better the results
3. **Pick your model** in the Settings tab (PhotoScribe auto-detects what's available in LM Studio or Ollama)
4. **Click Generate Metadata** — the AI works through each photo one by one
5. **Review everything** in the Results tab. You can edit titles, captions, and keywords directly before writing
6. **Click Write Metadata to Files** when you're happy

You can also **Export CSV** to review or bulk-edit a whole batch in a spreadsheet, then **Import CSV** to load your edits back in. Double-click any processed photo in the list to jump straight to its entry in the Results tab.

![Reviewing results with the photo preview](screenshot-results.png)

### Supported formats

**Standard:** JPEG, HEIC/HEIF (iPhone photos), TIFF, PNG, WebP

**RAW:** CR2, CR3 (Canon), NEF (Nikon), ARW (Sony), ORF (Olympus/OM System), RAF (Fujifilm), RW2 (Panasonic), PEF (Pentax), DNG, and more

### Where does the metadata go?

PhotoScribe writes to both IPTC and embedded XMP, which means it works with every major cataloguing application: **Lightroom Classic, Capture One, Photo Mechanic, Bridge, Finder,** and anything else that reads standard metadata.

**RAW files — XMP sidecars.** For RAW formats, enable *"Write to XMP sidecar for RAW files"* in Options and pick the naming convention your software expects:

- **Adobe / Lightroom / PhotoLab** — `photo.xmp` (extension replaced). Use for Lightroom Classic, Bridge, and **DxO PhotoLab**.
- **Darktable / DigiKam** — `photo.cr2.xmp` (extension kept). Use for darktable, digiKam, and similar DAM tools.

> **DxO PhotoLab users:** turn on *Preferences → "Synchronize metadata with XMP sidecars"* so PhotoLab reads the sidecar. Then, to pull in metadata PhotoScribe has written, select the photo(s) and choose **File → Metadata → Read Metadata From Image** — the title, description, and keywords will appear. PhotoLab scatters the standard IPTC fields across several panel sections rather than grouping them, so they're not all in one place: **Description** sits under *IPTC - Content*, **Title** under *IPTC - Status*, and keywords in the *Keywords* panel. It's all there — just spread around.

---

## Settings and options

### Batch context

Fill in Location, Event, Date/Time, and any notes before you process. The AI uses this to produce much more accurate and relevant results. For a landscape shoot, put the location. For an event, describe what it was. The difference in quality is significant.

### Folder context detection

Tick **Use folder context** and PhotoScribe reads your folder names to pre-fill the batch context. Dated names like `20250315 - Berry NSW` or `2025-03-15 Berry NSW` (and dot-separated variants) are parsed into Location and Date/Time — it walks up the directory tree, so nested folders still inherit the parent's context. It only fills fields you've left empty, so your manual input is never overwritten.

- **Use EXIF date as fallback** — when no date is in the folder name, reads `DateTimeOriginal` from the photo instead.
- **Look up location from GPS coordinates** *(opt-in, off by default)* — if a photo has GPS data, looks up a place name via OpenStreetMap. Because this sends coordinates to an external service, the first time you enable it PhotoScribe asks you to confirm. Everything else stays on your machine.

### Adding GPS to photos from cameras without it

Many cameras (the Fujifilm X-T4 among them) have no GPS, so their photos carry no coordinates. PhotoScribe can write them in two ways, both in the Batch Context card. Neither touches a photo that already has GPS.

For one place across the whole batch, type a place into Location and press **Look up** next to the GPS fields. PhotoScribe asks OpenStreetMap for the coordinates (the first time, it asks your permission, since the place name you typed leaves your computer) and fills in latitude and longitude. After that, lookups happen on their own a moment after you stop typing. You can also type or paste coordinates yourself, for example from a pin dropped in Apple or Google Maps. They're written to every photo when you press **Write Metadata to Files**.

To give each photo its own position, record a track on your phone while you shoot, with any app that exports GPX (Strava, Gaia GPS, myTracks and Geotag Photos Pro all do), then press **Load GPX…**. PhotoScribe matches each photo's capture time to the track and gives it the position you were at when you took it, interpolating between track points. The status line shows how many photos matched. A track match takes priority over the GPS fields, so photos outside the track still get the batch coordinates if you've set them.

Matching depends on the camera's clock. Cameras record local time without a time zone, so PhotoScribe uses the zone saved in the photo if there is one, and otherwise this computer's. If you travelled without changing the camera's zone, set **Camera clock** to the zone the camera was on. If nothing matches, that's almost always the cause. It's worth syncing the camera clock to your phone before a shoot; a camera running a few minutes fast places photos a few minutes along the track.

With **Look up location from GPS coordinates** ticked, matched photos also get a place name in their caption and keywords, exactly as photos from a GPS-equipped camera do.

### Folder presets

In the **Folder Presets** tab you can define rules: *if a folder name contains X, auto-apply a prompt preset or a keyword list.* Useful for recurring subjects — e.g. a folder containing "wedding" applies your Event preset and wedding vocabulary automatically. First matching rule wins.

### Prompts and presets

The default prompt works well for most photography, with built-in presets for Landscape, Event, and Product. Edit the prompt freely, then manage your own presets from the dropdown — **Save As** to create one, **Update** to overwrite, **Delete** to remove. Custom presets persist between sessions; the built-in ones can't be deleted, only overridden.

### Outputs: captions for different destinations

The **Caption written to the file** setting decides what the title, caption and keywords stored in the photo are for:

- **Catalogue**, the default: factual and searchable.
- **Stock**: literal titles, 25 to 45 keywords ordered most important first, no brand names or praise, and the editorial caption format when a photo shows people or brands.
- **Flickr**: Flickr reads the title, caption and keywords from the file on upload, so these are written as a gallery description and tags.

Tick any of Instagram, Facebook, Threads / Bluesky, Mastodon and Alt text under **Also write posts for**, then press **Write posts** on a photo in Results. The posts are written for that photo alone, without changing its title, caption or keywords, so you only spend the time on photos you'll actually share. Each has a copy button and a character count that turns red past the platform's limit, and they're included in CSV exports. Tick **Write posts for every photo during Generate** if you'd rather have them for the whole batch, which takes considerably longer. Posts are never written into the photo, because those platforms strip embedded metadata on upload and hashtags don't belong in your catalogue.

**Write posts in the first person** makes them read as though you're sharing your own photo. PhotoScribe keeps that first person to reactions to the photo and tells the model not to invent where you were, when, or how often you go there, but read posts before you use them, as you would anything a model writes.

### Keyword vocabulary

If you need consistent keywording across your catalogue, paste in your keyword list (one per line, or comma-separated). The AI will prefer terms from your vocabulary where applicable. You can also load a vocabulary from a text file.

### Options

- **Create backup files** — makes a `.original` copy of each file before writing. On by default. Turn it off once you trust the workflow
- **Append keywords** — adds new keywords to any existing ones rather than replacing them
- **Skip title/caption if already present** — useful for re-running to add keywords to already-captioned files
- **Describe people in photos** — instructs the AI to describe people's positions, roles, and actions (it never tries to identify anyone by name)
- **Auto-deduplicate similar keywords** — collapses plural and case variants so you don't end up with both "tree" and "trees"
- **Keyword density** — Fewer / Standard / More, if you want shorter or richer keyword sets

### Remote Ollama

If you have a separate machine with a large GPU (or a Spark), PhotoScribe can connect to Ollama running there instead of locally. Change the URL field in the Settings tab from `http://localhost:11434` to your server's IP, e.g. `http://192.168.1.50:11434`. Set `OLLAMA_HOST=0.0.0.0` on the remote machine and restart Ollama.

---

## Tips

- **Context matters more than model size.** Telling the AI "This is a landscape photo taken at sunrise on the South Coast of NSW, March 2026" gets you much better results than leaving the context fields empty.
- **Review before writing.** The AI is good but not infallible — especially with people's names, specific species, or locations it might not recognise. A quick scan in the Results tab takes seconds.
- **Backups are on by default.** Backup files have `.original` added to the filename. Once you've confirmed a few batches look good, you can safely turn backups off.
- **Apple Silicon is fast.** M-series Macs handle these models very efficiently. An M2 Pro with 16GB can process a photo in 5–10 seconds with the 12b model.
- **CSV export** is handy if you want to review a large batch in a spreadsheet before committing.

---

## Troubleshooting

**"Cannot connect to Ollama"**
Ollama isn't running. Click the Ollama icon in your menu bar to start it, or reinstall from [ollama.com](https://ollama.com). The status indicator at the top right of PhotoScribe will turn green once it connects.

**No models appear in the dropdown**
You haven't pulled a model yet. Open Terminal and run `ollama pull gemma4:12b-it-qat` (or `gemma4:e4b-it-qat` for a smaller one). PhotoScribe will detect it automatically — click Refresh next to the model dropdown.

**"ExifTool required" dialog**
Click the "Download Installer" button in the dialog — it takes you straight to the ExifTool macOS package. Install it, restart PhotoScribe, and the warning won't appear again.

**RAW files show a grey thumbnail / fail to load**
This is unusual as rawpy is bundled with the app. If it persists with a specific file, try exporting it as DNG from your camera software first.

**Generation is slow**
Almost always a model that's too big for your memory. PhotoScribe logs a warning when generation drops below 8 tokens a second, which is the tell. Switch to the model **Recommend Model** suggests; a smaller model that fits is several times faster than a larger one that doesn't. Writing posts for every photo during Generate also adds a lot of time, so use **Write posts** in Results for just the photos you'll share.

**Metadata doesn't appear in Lightroom after writing**
Lightroom caches metadata. Select the photos and choose **Metadata → Read Metadata from File** to force a re-read.

**Windows flags the installer as a virus / SmartScreen blocks it**
This is a **false positive**. The installer is built with PyInstaller, whose launcher is commonly misidentified by antivirus heuristics, and it isn't code-signed yet (free open-source signing via SignPath is being set up). The installer is built in the open on GitHub Actions from the source here — nothing is added by hand. To proceed: on the SmartScreen prompt click **More info → Run anyway**; if Defender quarantined it, allow/restore it under *Virus & threat protection → Protection history*. You can verify your download with `Get-FileHash .\PhotoScribe-Setup.exe` against the SHA-256 listed on the release.

---

## For developers

If you'd rather run from source (Windows/Linux or to contribute):

```bash
git clone https://github.com/repomonkey/PhotoScribe.git
cd PhotoScribe
./install.sh       # macOS/Linux — handles venv, dependencies, and launches
install.bat        # Windows
```

To build the macOS app bundle yourself:

```bash
./build_app.sh
```

This produces a signed and notarized `dist/PhotoScribe.dmg`. Requires Python 3.10–3.13 and your Apple Developer credentials stored via `xcrun notarytool store-credentials`.

The **Windows installer** is built by GitHub Actions on every `v*` tag (`.github/workflows/build-windows.yml`), which attaches `PhotoScribe-Setup.exe` to the release automatically. To build it locally on Windows: install the dependencies, run `fetch-exiftool.ps1`, build with `pyinstaller PhotoScribe-Windows.spec`, copy `exiftool.exe` and `exiftool_files\` into `dist\PhotoScribe\`, then compile `installer.iss` with [Inno Setup](https://jrsoftware.org/isinfo.php).

---

## Support

PhotoScribe is free and open source, and always will be. If it saves you time, you can support ongoing development on **[Patreon](https://www.patreon.com/c/AndyHutchinson)**, or with a one-off contribution via the **[anti-subscription page](https://andyhutchinson.com.au/the-anti-subscription/)** if you'd rather not sign up for anything. Entirely optional, and genuinely appreciated — and telling other photographers about it helps just as much.

## Licence

MIT

## Credits

Built by [Andy Hutchinson](https://andyhutchinson.com.au)

PhotoScribe uses [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai) for local AI inference and [ExifTool](https://exiftool.org) by Phil Harvey for metadata operations.
