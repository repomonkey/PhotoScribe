# Changelog

All notable changes to PhotoScribe are recorded here. Dates are ISO (YYYY-MM-DD).

## [1.7.0] — 2026-09-28

### Added
- **Captions for different destinations.** A new Outputs card chooses what the caption written into the photo is for. Catalogue is the existing factual style and stays the default. Stock writes literal titles, 25 to 45 keywords ordered most important first, no brand names or subjective praise, and uses the editorial caption format when a photo shows identifiable people or brands. Flickr writes a gallery-style description and tags, since Flickr reads them from the file on upload.
- **Ready-to-paste posts for Instagram, Facebook, Threads / Bluesky and Mastodon, plus alt text.** Tick the ones you want, then press **Write posts** on a photo in Results to write them for that photo, without touching its title, caption or keywords. Most photos in a batch never get posted, so this keeps Generate fast. To write them for every photo during Generate instead, tick *Write posts for every photo during Generate*. Each post has a copy button and a character count that turns red past that platform's limit, and posts are included in CSV exports and restored with saved progress. They are never written into the photo: those platforms strip embedded metadata on upload, and hashtags don't belong in a catalogue. Mastodon hashtags are written in CamelCase so screen readers can say them.
- **An option to write posts in the first person**, as the photographer sharing their own photo. It is limited to reactions to the photo itself; the model is told not to invent when you were there, what you did, or that it's a favourite spot, which it did readily in testing until told otherwise.
- **Copy buttons on Title, Caption and Keywords** in Results as well.
- **A warning when generation is crawling.** If the model writes fewer than 8 tokens a second, the log says so once and explains why: it almost always means the model is too big for the memory available and is being read back from disk as it runs. Measured on a 32GB M2 Max, Gemma 4 12B ran at 33 tokens a second and held it, while a 27B and a 26B-A4B that didn't fit ran at 3 to 6. Nothing errors in that state, so without the warning it just looks like PhotoScribe is slow.

### Changed
- **Recommend Model now suggests Gemma 4, and leaves real headroom.** It previously named Gemma 3 and counted 70% of a Mac's memory as usable, ignoring Lightroom and everything else running. It now recommends Gemma 4 E2B, E4B, 12B or the 26B-A4B mixture-of-experts model by total memory: 12B for 24 to 32GB Macs and 12 to 16GB graphics cards, 26B-A4B from 48GB or a 24GB card. 12GB cards were previously sent to the 4B, though the 12B fits them easily. On a Mac it now advises an MLX build in LM Studio rather than Q4_K_M, which is the GGUF term.
- When several models are available, a Gemma 4 12B is selected by default.
- The Results detail panel now scrolls, so a photo with several posts fits, and the preview no longer pushes the right-hand edge out of view.

### Internal
- The test suite no longer reads or writes your real PhotoScribe settings. The app opens its preferences with a Qt constructor that always uses the system store, so tests were loading live settings and failed once posts were ticked. They now get a throwaway settings file.

## [1.6.3] — 2026-07-19

### Fixed
- **Each photo now uses its own capture date, not the first photo's.** Loading two or more photos put the first one's date into the Date/Time field and applied it to every photo in the batch, so a folder spanning a morning and an evening had every frame labelled with the morning. The date is now read per photo when its metadata is generated, in the same way the location has been since 1.5.6. Anything typed into the Date/Time field still overrides it for the whole batch. Reported by a viewer on YouTube.
- **The time of day is now included and used.** Only the date was passed to the model before. The capture time is what settles whether a photo is sunrise or sunset, which is difficult to judge from the image alone, so it is now supplied along with the date and the model is told to use it for the light rather than to name the date in the caption.
- **Auto-detected context no longer survives "Clear All".** Anything PhotoScribe filled in for you is cleared along with the photos, so the next folder starts clean instead of inheriting the last one's date and location. Values you typed yourself are kept.
- **The capture date is read from XMP sidecars too**, not just the file, matching how GPS and location have worked since 1.5.4.

## [1.6.2] — 2026-07-19

### Added
- **Keywords now work in darktable.** Keywords are additionally written to `lr:hierarchicalSubject`, which is where darktable looks — it ignores the `dc:subject` field PhotoScribe was using, so keywords never appeared there even though titles and captions did. Written on every path: embedded files, XMP sidecars, and batch writes. Reported by a viewer on YouTube.
- **Keywords already applied in darktable are now read back**, so "append keywords" won't duplicate them and existing tags can inform the caption. Hierarchical tags are understood — `Places|Australia|Gerroa` is read as `Gerroa`.

### Fixed
- **Auto-tagger junk no longer leaks into your keywords and captions.** If a photo already carried tags, PhotoScribe told the model they were accurate and to keep them — reasonable for a few deliberate tags, but libraries that have been through an automatic tagger carry dozens of machine labels, and a beach in New South Wales was coming back tagged *Caribbean*, *Loch*, *Lake district* and *Rectangle*. How far those tags are trusted now depends on how many there are: a handful is treated as deliberate and used as before, so a species or place name from a specialist tagger still reaches the caption, while a large set is treated as unverified hints the model may draw on but must not copy wholesale. On a test photo carrying 45 automatic tags, the result went from 45 keywords including obvious nonsense to 13–15 accurate ones. Existing tags are still preserved in the file itself by "Append keywords to existing".

## [1.6.1] — 2026-07-10

### Fixed
- **"Couldn't read metadata" failures with reasoning models are fixed, and generation is much faster.** Newer models (Gemma 4 and similar) run a hidden "thinking" pass before answering. That reasoning was consuming the entire response budget — measured at 840–1200 tokens for a single photo — so the model was cut off before it finished writing its JSON, and the leftover scratchpad got reported as an invalid response. Reasoning is now switched off explicitly, which took a test photo from failing twice in a row to succeeding first time, and cut a request from roughly 1,400 tokens to 140.
- **A photo that fails is now retried with a bigger budget** rather than the same one that just ran out, and if a response really is cut short the log says so plainly instead of blaming the JSON.
- **Response length options raised** to 2048 / 4096 / 8192 tokens, so models that ignore the no-thinking flag still have room to finish.
- **The place name reliably reaches the title and caption again**, instead of only the keywords. The location was being framed defensively ("don't name a different place") rather than as an instruction, which a model without a reasoning pass tends not to act on — in testing the place name appeared in only 1 of 3 captions, and now appears in every one. It also now prefers the recognisable town or locality over a street address, so a Sublocation like "Geering Street 41" no longer ends up in the title.
- **No more duplicate XMP sidecars.** If a sidecar already existed under the other naming convention (`photo.xmp` vs `photo.cr2.xmp`), PhotoScribe wrote a second one — so Lightroom kept reading its own file, never saw the new metadata, and any star rating or colour label in it was stranded. It now writes to whichever sidecar already exists.

### Note on star ratings
PhotoScribe does not remove star ratings or colour labels — verified for embedded files and for existing sidecars. If ratings disappear after **Read Metadata from File**, it is because Lightroom replaces the catalogue's values with the file's, and the rating was only ever in the catalogue. Use **Metadata → Save Metadata to File** *before* running PhotoScribe and the rating will be in the sidecar, where it is then preserved.

## [1.6.0] — 2026-07-10

### Changed
- **Redesigned interface.** The window is reorganised into distinct cards — Model, Prompt, Batch Context, Options — each with a hairline border, rounded corners and its own section label, instead of one continuous scroll. The palette is warmer and far less saturated: the accent is now reserved for the things that actually mean something (the active tab, section labels, ticked checkboxes, and the Generate button), so nothing else competes for your eye.
- **Clearer controls.** Buttons share one height and radius and are ranked by weight: Generate is solid accent, Write Metadata is a muted teal, Export/Import are bordered ghost buttons. Checkboxes are a filled accent square when ticked and a plain outline when not. Tabs get a pill-shaped active state, and the backend connection status is now a pill badge in the header.
- **The prompt box is taller**, so the default prompt reads in full without scrolling.

### Fixed
- A stray white strip could appear at the right-hand edge of the settings panel, and the scrollbar rendered light against the dark theme.

## [1.5.6] — 2026-07-10

### Fixed
- **The location option no longer switches itself off between sessions.** "Look up location from photo GPS / metadata" was never saved, so it silently reset to unticked on every launch — while the one-time consent *was* remembered, making it look as though the option should still be on. It now persists, as do "Use folder context" and the EXIF-date fallback.
- **Location now takes effect when you enable it, and on Regenerate.** It used to be resolved once, when photos were loaded. Ticking the option afterwards, or re-running Regenerate, did nothing — you had to clear and reload the folder. The location is now resolved per photo when the metadata is generated.
- **Each photo gets its own location.** Previously one location was sampled from the first few files and applied to the whole batch, so a folder spanning several places tagged them all identically — and a geotagged photo further down the list was never even looked at. Anything typed into the Location field still overrides everything, for every photo.
- **Rural and coastal places are named properly.** The geocoder ignored OpenStreetMap's `locality` and `neighbourhood` fields, so a beach at Gerroa, NSW came back as the useless "New South Wales, Australia". It now reads them, giving "Gerroa, New South Wales, Australia".
- **The lookup says what it found.** It logs the location resolved for each photo, and says so once when a photo has no GPS or place name at all, instead of failing silently.

### Changed
- **Geocoding results are cached and rate-limited.** Coordinates are rounded (~11m) and cached, so a folder shot in one place makes a single request, and requests are held to one per second in line with OpenStreetMap Nominatim's usage policy.

## [1.5.5] — 2026-07-10

### Fixed
- **"Look up location from GPS" now actually runs on its own.** The location lookup was only executed when **Use folder context** was *also* enabled — with folder context off, enabling the GPS/location option did nothing at all (no network request, no location filled). Each source now runs on its own checkbox independently: folder-name detection, the EXIF-date fallback, and the location lookup no longer depend on one another. Reported on GitHub (#18).

### Changed
- **Location lookup renamed and clarified.** The option is now **"Look up location from photo GPS / metadata"**: it fills the Location field from the place name your cataloguer (e.g. Lightroom) already resolved — City/State/Country, from the file or its `.xmp` sidecar, with no network request — and only reverse-geocodes the GPS coordinates via OpenStreetMap when there's no such place name. The tooltip and consent dialog now describe this.

## [1.5.4] — 2026-07-10

### Fixed
- **Location from RAW/DNG photos is no longer missed, so captions stop inventing far-away places.** PhotoScribe now reads GPS *and* the resolved place name (Sublocation / City / State / Country) from a photo's `.xmp` sidecar, not just from the file itself. RAW files geotagged in Lightroom or Geotag Photos Pro keep that data in the sidecar, so previously a RAW/DNG shot arrived with no location and the model would free-associate from the image alone — e.g. a Gaudí tower in Comillas, Spain captioned as being in Phuket, Thailand. JPEGs (GPS baked into EXIF) always worked; RAW now behaves the same. Reported on GitHub.
- **The resolved place name is now preferred over a GPS lookup.** When a cataloguer (e.g. Lightroom) has already written City/State/Country, PhotoScribe uses that directly — exact and no network call — and only falls back to reverse-geocoding GPS coordinates when there's no place name to use.
- **Accented keywords no longer show as "?" in Lightroom.** Keywords like *Château de Chenonceau* were written as Latin-1 and mis-decoded by readers. The IPTC block is now marked UTF-8 (`CodedCharacterSet=UTF8`) on every write, so accents survive round-trip.

### Changed
- **The photo's location is now treated as ground truth in the prompt.** When a Location is supplied, the model is explicitly told the photo was taken there and must not name a different city, region, or country even if the scene reminds it of somewhere else — and to describe the scene without naming a place when it isn't sure, rather than guessing.

## [1.5.3] — 2026-07-09

### Added
- **Regenerate photos after they're already done.** Right-click a photo in the list for **Regenerate this photo** or **Regenerate all photos** — it resets them and runs generation again with your current settings. Previously a processed (green-dot) photo was skipped by Generate with no way to redo it, so changing a setting (e.g. turning on GPS location lookup) meant it wouldn't take effect on already-processed photos. Requested on GitHub.

## [1.5.2] — 2026-07-06

### Fixed
- **Models are now forced to return JSON, so "couldn't read metadata" failures should essentially stop.** Some models (e.g. Gemma via LM Studio) would occasionally reply with a bulleted "thinking" plan and never actually produce the JSON — which no amount of parsing can recover. PhotoScribe now uses the backend's structured-output mode (LM Studio / OpenAI `json_schema`, Ollama schema `format`) to grammar-constrain the reply to the required `{title, caption, keywords}` shape, so the model can't wander off into prose. Falls back gracefully if a backend doesn't support it, and the tolerant parser from 1.5.1 remains as a safety net. Verified against Gemma-4-12B in LM Studio.

### Changed
- **Often much faster, too.** Because the model no longer spends time writing a reasoning essay before the JSON, generation is frequently several times quicker — in testing on the same model, roughly **7s per photo instead of ~65s**. A pleasant side effect of the fix above.

## [1.5.1] — 2026-07-06

### Fixed
- **Far fewer "couldn't read metadata" failures.** The model's reply is now parsed much more forgivingly. Previously a photo could fail if the model wrapped its JSON in a "thinking" preamble, used single quotes or smart quotes, left a trailing comma, added `//` comments, or left keywords unquoted (`[gulls, beach]`) — all common with smaller local models. The parser now scans for the real JSON object (ignoring stray braces in surrounding prose) and repairs these common malformations before giving up. When it genuinely can't parse a reply, the raw response is now written to the log so the failure can be diagnosed.

## [1.5.0] — 2026-07-06

### Changed
- **Much faster metadata writing.** Writing a folder now uses a single persistent ExifTool process (`-stay_open` batch mode) instead of launching a new one per photo — roughly **3× faster** on large folders (measured 47 photos in 33s vs 102s), and on Windows it no longer steals window focus for every file. Skip-existing, append-keywords, and replace all behave exactly as before, including the numeric-keyword handling from 1.4.2. RAW/XMP-sidecar files are still written individually. Contributed by [@bridgew99](https://github.com/bridgew99) (#16), closing the batch-writing request in #13.

### Internal
- Added tests covering the batch write worker across replace/append/skip, plus the metadata read/write path generally (`tests/test_metadata.py`).

## [1.4.2] — 2026-07-05

### Fixed
- **Writing metadata no longer fails on files with numeric keywords.** If a photo already had a purely numeric keyword (e.g. a year like `2025`), writing aborted with `'int' object has no attribute 'lower'` — ExifTool returns numeric values as numbers, not text, which broke the keyword comparison. Keywords are now normalised to text everywhere, whether read from the file or generated by the model, so the write goes through. Affected RAW/XMP-sidecar files (e.g. `.ORF`) as well as embedded metadata. Thanks to the user who reported it with the exact error.

## [1.4.1] — 2026-07-04

### Fixed
- **Stop the model inventing specific names it can't know.** On subjects like landmarks or bridges, small vision models would confabulate a plausible-but-wrong proper name to fill the gap — and pick a different one for each near-identical frame (four burst photos of one bridge → four different, mostly-wrong bridge names). PhotoScribe now instructs the model to only name specifics that are given in the context/tags or clearly legible in the image, and to stay generic ("a bridge over a river") when it isn't sure — better general and correct than specific and wrong. Setting the **Location** field gives it a real place name to anchor to. Reported by @Zoolander06 in #15.

## [1.4.0] — 2026-07-04

### Added
- **Person-aware captions.** When "Describe people in photos" is on, PhotoScribe reads any names already tagged on a photo — from `PersonInImage`, MWG face regions, and Excire FaceTags, in the image *and* its co-located XMP sidecar — and weaves them into the title, caption, and keywords ("Andy and his dog on a coastal trail" instead of "a man with a dog"). Only kicks in when names are present; photos with no tags behave as before. Based on [@Boui3D](https://github.com/Boui3D)'s reference in #9.
- **Species / subject-aware captions.** PhotoScribe now reads keyword and subject tags already on a file (and its XMP sidecar) — e.g. bird names written by a specialist tagger like SuperPicky — and feeds them to the model as facts. Local generalist vision models can't reliably identify a species, so the caption can now say "a Superb Fairywren perched in reeds" instead of "a small bird". Off when there are no existing tags.
- **"Kept" title/caption shown in Results.** With "Skip title/caption if file already has them" turned on, the Results panel now shows the *existing* title/caption (marked **KEPT**) — the value that will actually stay on the file — instead of the AI's discarded suggestion. This clears up the common confusion where it looked like skip wasn't working.

### Changed
- **Model recommender now detects AMD and Intel GPUs** (Windows/Linux) and Intel-Mac discrete cards, not just NVIDIA and Apple Silicon — so it shows your actual GPU and uses its VRAM where it can read it, instead of always falling back to system RAM.

## [1.3.4] — 2026-06-30

### Added
- **Auto-write after generating** — a new option writes metadata to your files as soon as generation finishes, so a large folder can run fully unattended.
- **Live ETA** on the progress bar while generating (estimated time remaining).
- **Version number** shown under the title.

### Fixed
- **Windows: ExifTool no longer steals focus.** Each ExifTool call was popping a console window that grabbed focus mid-write; that window is now suppressed.
- **Ignore macOS AppleDouble (`._*`) and hidden files** when loading. On external/network volumes these were loaded as phantom photos (doubling the count and wasting processing).

## [1.3.3] — 2026-06-28

### Added
- **The keyword vocabulary now persists** between sessions — paste it once and it's there next launch.

### Changed
- **Larger photo preview** in the Results tab (roughly double), so it's actually useful for reviewing before writing.
- **Clear All** now also deletes the folder's saved-progress file, so repeated test runs start clean without hunting down a hidden file.

## [1.3.2] — 2026-06-28

### Fixed
- **RAW files: skip-existing (and append-keywords) now work for XMP sidecars.** The sidecar writer ignored both options and always overwrote, so existing captions on RAW files (e.g. from Photo Mechanic) were replaced even with "skip if already present" turned on. It now reads the existing sidecar and honours skip/append like the embedded path. Follow-up to 1.3.1, which only covered embedded JPEG/TIFF metadata.

## [1.3.1] — 2026-06-24

### Fixed
- **"Skip title/caption if file already has them" no longer overwrites existing captions.** The check only looked at the IPTC caption field; captions stored in XMP (`dc:description`) or EXIF — as Photo Mechanic and Lightroom write them — went undetected and were overwritten. It now coalesces title, caption, and keywords across IPTC, XMP, and EXIF.

## [1.3.0] — 2026-06-24

A big feature release, with substantial community contributions from
[@bridgew99](https://github.com/bridgew99) (performance, context detection, UI/UX).

### Added
- **Folder context detection** — parses dated folder names (e.g. `20250315 - Berry NSW`, ISO and dot-separated variants), walking up the tree, to pre-fill Location and Date/Time (empty fields only).
- **Folder Presets tab** — rules to auto-apply a prompt preset or keyword list based on folder name.
- **Editable prompt presets** — Save As / Update / Delete; custom presets persist, built-ins can be overridden.
- **CSV import** — load metadata from a previously exported (or spreadsheet-edited) CSV; matches by filepath then filename and reports unmatched rows.
- **Photo preview** in the Results tab, and **double-click** a photo to jump to its result.
- **Model recommender** — detects GPU/RAM and suggests the best Gemma model (LM Studio name or Ollama pull).
- **Describe people** option — describe positions/roles/actions, never names.
- **Auto-deduplicate keywords** — removes plural/case near-duplicates.
- **EXIF date fallback** when no folder date is detected.
- **GPS reverse-geocode** (opt-in, off by default, one-time consent) — place-name lookup via OpenStreetMap; reads the image and co-located XMP sidecar.
- **Progress resume** — interrupted batches (and manual edits) restore on reload.
- **Batch timing** — per-photo seconds in the log plus a batch summary in the status bar.

### Changed
- **Much faster** large-folder handling: no thumbnail generation at load, pipelined image pre-encoding, single-row table updates, threaded metadata writing with a progress bar.
- Options laid out in two columns; window sizes to the screen and centres on launch.

## [1.2.4] — 2026-06-22

### Added
- **DxO PhotoLab support** for RAW XMP sidecars. PhotoLab uses the same `photo.xmp` convention as Adobe/Lightroom, so the sidecar naming option is now labelled **"Adobe / Lightroom / PhotoLab"**. Title, description, and keywords round-trip into PhotoLab (with its *"Synchronize metadata with XMP sidecars"* preference enabled). No write-logic change — purely confirmation + labelling.

## [1.2.3] — 2026-06-22

### Fixed
- Keywords now **match an existing keyword vocabulary's spelling**. Generated keywords are snapped (case-insensitively) to the exact term from your keyword list, and case-insensitive duplicates are dropped. Stops Lightroom and other DAMs importing `sunset` as a new keyword separate from your existing `Sunset`.

## [1.2.2] — 2026-06-22

First cross-platform release: macOS **and** Windows.

### Added
- **Windows installer** (`PhotoScribe-Setup.exe`), built by GitHub Actions on every `v*` tag and attached to the release automatically. ExifTool is bundled, so Windows users have nothing else to install.
- New custom app icon across both platforms.

### Changed
- Reworked `MetadataWriter.find_exiftool()` so a frozen build looks for ExifTool **next to the app executable** first (where it now ships), then `_MEIPASS`, then PATH / known locations.
- Carried HEIC/HEIF support through to the Windows build.

## [1.2.1] — 2026-06-22

Reliability release for RAW/DNG handling and error reporting.

### Added
- **HEIC / HEIF support** via `pillow-heif` (iPhone photos). Added to supported formats, the file dialog, and the drop-zone label; libheif is bundled in the app.

### Changed
- RAW files (incl. DNG) are now decoded from their **embedded preview JPEG** first, falling back to a full LibRaw decode only when there's no usable preview. Fixes phone/HDR/Lightroom-converted DNGs that decoded dark or blank and made the model return nothing. Also faster.

### Fixed
- An empty or non-JSON model response now triggers **one automatic retry** and, if it still fails, a plain-English message — instead of the cryptic `JSON parse error: Expecting value: line 1 column 1 (char 0)`.

## [1.2] — 2026-06-22

### Added
- **XMP sidecar support** for RAW files, with a selectable naming convention:
  - Adobe / Lightroom — `photo.xmp` (extension replaced)
  - Darktable / DigiKam — `photo.cr2.xmp` (extension kept)
  Title, caption, and keywords are written to the correct `XMP-dc` fields.
- **Keyword density** control (Fewer / Standard / More keywords) — no technical token settings exposed.

### Fixed
- LM Studio "thinking" models occasionally returning empty responses, by improving how JSON is extracted (and falling back to `reasoning_content`).

## [1.1] — 2026-06-07
- Windows build pipeline scaffolding and cross-platform ExifTool detection (the pipeline itself was first made to actually work in 1.2.2 — see notes below).

## [1.0.0]
- Initial release: local AI photo metadata generation (LM Studio / Ollama), IPTC + embedded XMP writing via ExifTool, batch context, prompts/presets, keyword vocabulary, CSV export.

---

## Build & infrastructure notes

Engineering details worth remembering, especially for the Windows pipeline.

### macOS build
- `./build_app.sh` produces a signed + notarized `dist/PhotoScribe.dmg` and `.app`.
- The app icon is the committed `PhotoScribe.icns` (regenerated from `icon.png`); `build_app.sh` only falls back to generating one from `logo.png` if `PhotoScribe.icns` is absent.
- `logo.png` is the gold footer signature shown in-app — **not** the icon. Don't repurpose it.

### Windows build (`.github/workflows/build-windows.yml`)
Hard-won fixes:

1. **ExifTool download.** exiftool.org no longer self-hosts the Windows zip — it's on SourceForge. `fetch-exiftool.ps1` reads the current version from `https://exiftool.org/ver.txt` and downloads `exiftool-<ver>_64.zip` from SourceForge. SourceForge serves the real file only to non-browser user agents, so the script sends a `curl/8.4.0` UA (a browser-like UA gets an HTML interstitial instead).

2. **`exiftool_files` layout.** Modern ExifTool is not a single exe — it's `exiftool(-k).exe` (a launcher, renamed to `exiftool.exe`) plus an `exiftool_files\` folder with the Perl runtime. Bundling this **through** PyInstaller breaks Perl's `@INC` (`Can't locate strict.pm`). Fix: do **not** put ExifTool in the spec `datas`; instead copy `exiftool.exe` + `exiftool_files\` into `dist\PhotoScribe\` (next to the exe) as a post-build step, and have `find_exiftool()` look there.

3. **CI release upload.** The workflow needs `permissions: contents: write` or `gh release upload` fails with `HTTP 403: Resource not accessible by integration`.

4. **Triggers.** Builds on `v*` tags and manual `workflow_dispatch`. Manual runs upload the installer as a downloadable artifact; tag runs also attach it to the matching release. Installer version comes from the tag (falls back to `0.0.0` on non-tag runs).

Local Windows build (Python 3.10–3.13):
```
pip install -r requirements.txt "pyinstaller>=6.0"
powershell -ExecutionPolicy Bypass -File fetch-exiftool.ps1
pyinstaller PhotoScribe-Windows.spec --noconfirm --clean
Copy-Item exiftool.exe dist\PhotoScribe\ -Force
Copy-Item exiftool_files dist\PhotoScribe\exiftool_files -Recurse -Force
& "<path>\ISCC.exe" /DMyAppVersion=<version> installer.iss
```
