"""Tests for the metadata read/write path.

Covers the code that actually touches users' files — keyword normalisation,
existing-metadata parsing, prompt construction, and end-to-end ExifTool
writes across the append/skip/replace matrix. Includes a regression test for
the v1.4.2 numeric-keyword crash ('int' object has no attribute 'lower').

The write tests are light integration tests against a real ExifTool (skipped
if it isn't available); the rest are pure units, some using a stubbed _run.
"""
import os
import sys
import json
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
import photoscribe
from photoscribe import (MetadataWriter, OllamaWorker, MetadataWriteWorker,
                         PhotoMetadata, PhotoItem)


HAVE_EXIFTOOL = MetadataWriter.find_exiftool() is not None
needs_exiftool = pytest.mark.skipif(
    not HAVE_EXIFTOOL, reason="ExifTool not available"
)


class FakeResult:
    """Stand-in for subprocess.CompletedProcess."""
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def make_worker(**kw):
    kw.setdefault("photos", [])
    kw.setdefault("model", "m")
    kw.setdefault("prompt", "Describe the photo.")
    kw.setdefault("context", "")
    kw.setdefault("ollama_url", "")
    kw.setdefault("keywords_list", [])
    return OllamaWorker(**kw)


def make_jpeg(path):
    Image.new("RGB", (32, 32), (100, 140, 180)).save(str(path), "JPEG")
    return path


@pytest.fixture(scope="session", autouse=True)
def _isolated_settings(tmp_path_factory):
    """Keep the test run away from the developer's real PhotoScribe settings.

    The window loads its preferences on construction via
    QSettings("PhotoScribe", "PhotoScribe"), and that constructor always opens
    the native store (Qt ignores setDefaultFormat for it). So tests read, and
    some once wrote, the live settings: they failed as soon as real posts were
    ticked, and a GPS setting leaked into live prefs. Swap in a QSettings that
    sends those named settings to a throwaway .ini instead."""
    from PySide6.QtCore import QSettings
    folder = str(tmp_path_factory.mktemp("qsettings"))

    class _ThrowawaySettings(QSettings):
        def __init__(self, *args, **kwargs):
            if len(args) == 2 and all(isinstance(a, str) for a in args):
                super().__init__(os.path.join(folder, f"{args[0]}-{args[1]}.ini"),
                                 QSettings.IniFormat)
            else:
                super().__init__(*args, **kwargs)

    patch = pytest.MonkeyPatch()
    patch.setattr(photoscribe, "QSettings", _ThrowawaySettings)
    yield
    patch.undo()


@pytest.fixture(scope="session")
def qapp():
    """A QApplication for tests that instantiate QThread workers."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ── _norm_keywords ────────────────────────────────────────────────

class TestNormKeywords:
    def test_none_and_empty(self):
        assert MetadataWriter._norm_keywords(None) == []
        assert MetadataWriter._norm_keywords([]) == []

    def test_coerces_numeric(self):
        # ExifTool -j returns a purely-numeric keyword (a year) as an int
        assert MetadataWriter._norm_keywords([2025, "beach"]) == ["2025", "beach"]

    def test_drops_blanks_and_none(self):
        assert MetadataWriter._norm_keywords(["a", "", "  ", None, "b"]) == ["a", "b"]

    def test_strips_whitespace(self):
        assert MetadataWriter._norm_keywords(["  sunset  "]) == ["sunset"]


# ── OllamaWorker._clean_keywords ──────────────────────────────────

class TestCleanKeywords:
    def test_numeric_keyword_from_model(self):
        # A model may emit a bare number; must not raise, must stringify
        w = make_worker()
        assert w._clean_keywords(["Sunset", 2025, "beach"]) == ["Sunset", "2025", "beach"]

    def test_drops_blanks_and_none(self):
        w = make_worker()
        assert w._clean_keywords([" ", None, "ocean"]) == ["ocean"]

    def test_case_dedup_keeps_first(self):
        w = make_worker()
        assert w._clean_keywords(["Sunset", "sunset"]) == ["Sunset"]

    def test_snaps_to_vocabulary_spelling(self):
        w = make_worker(keywords_list=["Sunset", "Beach"])
        assert w._clean_keywords(["sunset", "beach"]) == ["Sunset", "Beach"]


# ── OllamaWorker._parse_response (tolerant JSON) ──────────────────

class TestParseResponse:
    def w(self):
        return OllamaWorker(photos=[], model="m", prompt="p", context="",
                            ollama_url="", keywords_list=[])

    def test_clean_json(self):
        d = self.w()._parse_response(
            '{"title":"A","caption":"B","keywords":["x","y"]}')
        assert d == {"title": "A", "caption": "B", "keywords": ["x", "y"]}

    def test_fenced_block(self):
        d = self.w()._parse_response(
            '```json\n{"title":"A","caption":"B","keywords":["x"]}\n```')
        assert d["title"] == "A"

    def test_reasoning_preamble_with_stray_brace(self):
        # A 'thinking' dump with a stray {brace} before the real object
        txt = ('Let me think. The image {shows} gulls.\nJSON:\n'
               '{"title":"Gulls","caption":"On a beach.","keywords":["gulls"]}\n'
               'Hope that helps!')
        d = self.w()._parse_response(txt)
        assert d["title"] == "Gulls" and d["keywords"] == ["gulls"]

    def test_trailing_commas(self):
        d = self.w()._parse_response(
            '{"title":"A","caption":"B","keywords":["x","y",],}')
        assert d["keywords"] == ["x", "y"]

    def test_single_quoted_dict(self):
        d = self.w()._parse_response(
            "{'title':'A','caption':'B','keywords':['x']}")
        assert d["title"] == "A"

    def test_smart_quotes(self):
        d = self.w()._parse_response(
            '{“title”:“A”,“caption”:“B”,'
            '“keywords”:[“x”]}')
        assert d["title"] == "A"

    def test_line_comments(self):
        d = self.w()._parse_response(
            '{\n"title":"A", // note\n"caption":"B",\n"keywords":["x"]\n}')
        assert d["caption"] == "B"

    def test_unquoted_keyword_array(self):
        d = self.w()._parse_response(
            '{"title":"A","caption":"B","keywords":[gulls, beach]}')
        assert d["keywords"] == ["gulls", "beach"]

    def test_brackets_in_caption_not_mangled(self):
        # Valid JSON with brackets inside a string must parse untouched — the
        # aggressive bare-array repair must not run when strict parse succeeds.
        d = self.w()._parse_response(
            '{"title":"A","caption":"Shot [at dusk] here","keywords":["x"]}')
        assert d["caption"] == "Shot [at dusk] here"

    def test_unrecoverable_returns_none(self):
        assert self.w()._parse_response("total gibberish, no json") is None
        assert self.w()._parse_response("") is None


# ── Structured output on the backend calls (mocked network) ───────

class _Resp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload or {"choices": [{"message": {"content": "{}"}}],
                                    "message": {"content": "{}"}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestStructuredOutput:
    def _worker(self, backend):
        return OllamaWorker(photos=[], model="m", prompt="p", context="",
                            ollama_url="http://x", keywords_list=[],
                            backend=backend)

    def test_openai_requests_json_schema(self, monkeypatch):
        seen = []
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: (seen.append(json), _Resp())[1])
        self._worker("openai")._call_openai("imgb64", "prompt")
        rf = seen[0].get("response_format")
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"]["required"] == ["title", "caption", "keywords"]

    def test_openai_falls_back_when_unsupported(self, monkeypatch):
        seen = []

        def post(url, json=None, timeout=None):
            seen.append(json)
            if "response_format" in json:
                return _Resp(status=400, text="'response_format.type' must be json_schema")
            return _Resp()
        monkeypatch.setattr(photoscribe.requests, "post", post)
        self._worker("openai")._call_openai("img", "p")
        assert len(seen) == 2 and "response_format" not in seen[1]

    def test_ollama_requests_format_schema(self, monkeypatch):
        seen = []
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: (seen.append(json), _Resp())[1])
        self._worker("ollama")._call_ollama("img", "p")
        fmt = seen[0].get("format")
        assert isinstance(fmt, dict) and fmt["required"] == ["title", "caption", "keywords"]

    def test_ollama_falls_back_to_json_then_plain(self, monkeypatch):
        seen = []

        def post(url, json=None, timeout=None):
            seen.append(json.get("format"))
            # reject the schema (dict) with 400, accept "json"
            if isinstance(json.get("format"), dict):
                return _Resp(status=400)
            return _Resp()
        monkeypatch.setattr(photoscribe.requests, "post", post)
        self._worker("ollama")._call_ollama("img", "p")
        assert seen[0] and isinstance(seen[0], dict)   # first: schema
        assert seen[1] == "json"                       # fallback: plain json mode


# ── Reasoning models exhausting the token budget ──────────────────

class TestReasoningBudget:
    """v1.6.1: reasoning models (Gemma 4) spend the whole max_tokens budget on
    a thinking pass before writing any JSON, so the answer gets truncated and
    the leftover scratchpad was reported as 'not valid JSON'."""

    def _worker(self, **kw):
        kw.setdefault("backend", "openai")
        return OllamaWorker(photos=[], model="m", prompt="p", context="",
                            ollama_url="http://x", keywords_list=[], **kw)

    def test_reasoning_disabled_in_request(self, monkeypatch):
        seen = []
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: (seen.append(json), _Resp())[1])
        self._worker()._call_openai("img", "p")
        assert seen[0].get("reasoning_effort") == "none"

    def test_drops_reasoning_effort_if_rejected(self, monkeypatch):
        seen = []

        def post(url, json=None, timeout=None):
            seen.append(dict(json))
            if "reasoning_effort" in json:
                return _Resp(status=400, text="unknown parameter reasoning_effort")
            return _Resp()
        monkeypatch.setattr(photoscribe.requests, "post", post)
        self._worker()._call_openai("img", "p")
        assert "reasoning_effort" in seen[0]
        assert "reasoning_effort" not in seen[1]
        # response_format must survive the reasoning_effort fallback
        assert "response_format" in seen[1]

    def test_truncated_reasoning_reports_token_limit(self, monkeypatch):
        # content empty + finish_reason "length" = ran out of budget mid-think.
        payload = {"choices": [{"finish_reason": "length",
                                "message": {"content": "",
                                            "reasoning_content": "*  Let me think..."}}]}
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: _Resp(payload=payload))
        with pytest.raises(RuntimeError) as e:
            self._worker()._call_openai("img", "p")
        msg = str(e.value).lower()
        assert "ran out of tokens" in msg
        assert "response length" in msg          # tells the user what to change

    def test_reasoning_content_still_used_when_not_truncated(self, monkeypatch):
        # Not truncated, so the scratchpad is the only thing on offer — the
        # tolerant parser may still find JSON in it.
        payload = {"choices": [{"finish_reason": "stop",
                                "message": {"content": "",
                                            "reasoning_content": '{"title":"t"}'}}]}
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: _Resp(payload=payload))
        assert self._worker()._call_openai("img", "p") == '{"title":"t"}'


# ── read_existing_metadata (stubbed _run) ─────────────────────────

class TestReadExistingMetadata:
    def _stub(self, monkeypatch, entry):
        monkeypatch.setattr(
            photoscribe, "_run",
            lambda *a, **k: FakeResult(stdout=json.dumps([entry]))
        )

    def test_numeric_keyword_regression(self, monkeypatch):
        # v1.4.2: a numeric keyword arrives as a JSON int; must come back as str
        self._stub(monkeypatch, {"Subject": [2025, "beach"]})
        title, caption, kws = MetadataWriter.read_existing_metadata("x.orf.xmp")
        assert kws == ["2025", "beach"]
        assert all(isinstance(k, str) for k in kws)

    def test_single_numeric_keyword(self, monkeypatch):
        # A lone numeric value isn't a list — must still normalise to [str]
        self._stub(monkeypatch, {"Keywords": 2025})
        _, _, kws = MetadataWriter.read_existing_metadata("x.jpg")
        assert kws == ["2025"]

    def test_iptc_xmp_exif_coalesce(self, monkeypatch):
        self._stub(monkeypatch, {"ObjectName": "T", "Description": "C",
                                 "Keywords": ["a", "b"]})
        title, caption, kws = MetadataWriter.read_existing_metadata("x.jpg")
        assert (title, caption, kws) == ("T", "C", ["a", "b"])

    def test_caption_falls_back_to_exif(self, monkeypatch):
        self._stub(monkeypatch, {"ImageDescription": "from exif"})
        _, caption, _ = MetadataWriter.read_existing_metadata("x.jpg")
        assert caption == "from exif"

    def test_empty_when_nonzero_returncode(self, monkeypatch):
        monkeypatch.setattr(photoscribe, "_run",
                            lambda *a, **k: FakeResult(returncode=1))
        assert MetadataWriter.read_existing_metadata("x.jpg") == ("", "", [])


# ── OllamaWorker._build_prompt ────────────────────────────────────

class TestBuildPrompt:
    def test_anti_confabulation_always_present(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        w = make_worker(describe_people=False)
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        prompt = w._build_prompt(photo)
        assert "invent" in prompt.lower()
        assert "general and correct than specific and wrong" in prompt

    def test_named_people_injected(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: ["Andy"])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        w = make_worker(describe_people=True)
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        prompt = w._build_prompt(photo)
        assert "People named in this photo: Andy" in prompt

    def test_existing_tags_injected(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords",
                            lambda f: ["Superb Fairywren"])
        w = make_worker(describe_people=False)
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        prompt = w._build_prompt(photo)
        assert "Superb Fairywren" in prompt

    def test_few_tags_are_trusted(self, monkeypatch):
        # v1.6.2: a handful of tags reads as deliberate (specialist tagger or
        # the photographer) and should still be trusted — this is the feature
        # from issue #9 that lets a caption say "Superb Fairywren".
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords",
                            lambda f: ["Superb Fairywren", "Werri Beach"])
        w = make_worker(describe_people=False)
        prompt = w._build_prompt(PhotoItem(filepath="x.jpg", filename="x.jpg"))
        assert "applied deliberately" in prompt
        assert "Superb Fairywren" in prompt
        assert "keep them in the keywords" in prompt

    def test_many_tags_are_downgraded_to_hints(self, monkeypatch):
        # An auto-tagged library carries dozens of machine labels; asserting
        # they're accurate launders them into the user's metadata.
        tags = [f"tag{i}" for i in range(40)]
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: tags)
        w = make_worker(describe_people=False)
        prompt = w._build_prompt(PhotoItem(filepath="x.jpg", filename="x.jpg"))
        assert "automatically generated" in prompt
        assert "Do not copy this list into the keywords" in prompt
        assert "applied deliberately" not in prompt

    def test_hint_list_is_capped(self, monkeypatch):
        tags = [f"tag{i}" for i in range(40)]
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: tags)
        w = make_worker(describe_people=False)
        prompt = w._build_prompt(PhotoItem(filepath="x.jpg", filename="x.jpg"))
        assert "tag0" in prompt
        # only _MAX_TAG_HINTS of them, so the tail must not appear
        assert "tag39" not in prompt
        assert "tag12" not in prompt

    def test_boundary_between_trusted_and_hinted(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        limit = OllamaWorker._DELIBERATE_TAG_LIMIT
        monkeypatch.setattr(MetadataWriter, "read_keywords",
                            lambda f: [f"t{i}" for i in range(limit)])
        w = make_worker(describe_people=False)
        assert "applied deliberately" in w._build_prompt(
            PhotoItem(filepath="x.jpg", filename="x.jpg"))
        monkeypatch.setattr(MetadataWriter, "read_keywords",
                            lambda f: [f"t{i}" for i in range(limit + 1)])
        assert "automatically generated" in w._build_prompt(
            PhotoItem(filepath="x.jpg", filename="x.jpg"))

    def test_tags_deduped_against_persons(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: ["Andy"])
        monkeypatch.setattr(MetadataWriter, "read_keywords",
                            lambda f: ["Andy", "beach"])
        w = make_worker(describe_people=True)
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        prompt = w._build_prompt(photo)
        # "Andy" appears in the people line, not repeated in the subjects line
        assert "already been tagged with: beach" in prompt

    def test_location_asserted_as_ground_truth(self, monkeypatch):
        # v1.5.4: when a Location is supplied, the prompt must forbid the model
        # naming a different place (the "Spain photo captioned Thailand" bug).
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        w = make_worker(describe_people=False,
                        context="Location: Comillas, Cantabria, Spain")
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        prompt = w._build_prompt(photo)
        assert "Comillas, Cantabria, Spain" in prompt
        assert "ground truth" in prompt.lower()
        assert "never name a different" in prompt.lower()

    def test_no_ground_truth_line_without_context(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        w = make_worker(describe_people=False, context="")
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        prompt = w._build_prompt(photo)
        assert "ground truth" not in prompt.lower()


# ── End-to-end writes (real ExifTool) ─────────────────────────────

@needs_exiftool
class TestWriteEmbedded:
    def _kw(self, path):
        _, _, kws = MetadataWriter.read_existing_metadata(str(path))
        return kws

    def test_numeric_existing_keyword_append_regression(self, tmp_path):
        # v1.4.2: appending onto a file that already has a numeric keyword
        # used to crash with 'int' object has no attribute 'lower'
        img = make_jpeg(tmp_path / "p.jpg")
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-IPTC:Keywords=2025", "-IPTC:Keywords=beach", str(img)],
                         capture_output=True, text=True)
        meta = PhotoMetadata(title="T", caption="C", keywords=["ocean"])
        ok = MetadataWriter.write_metadata(str(img), meta, backup=False,
                                           append_keywords=True)
        assert ok is True
        kws = [k.lower() for k in self._kw(img)]
        assert "2025" in kws and "beach" in kws and "ocean" in kws

    def test_replace_keywords(self, tmp_path):
        img = make_jpeg(tmp_path / "p.jpg")
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-IPTC:Keywords=old", str(img)],
                         capture_output=True, text=True)
        meta = PhotoMetadata(title="T", caption="C", keywords=["new"])
        MetadataWriter.write_metadata(str(img), meta, backup=False,
                                      append_keywords=False)
        kws = [k.lower() for k in self._kw(img)]
        assert "new" in kws and "old" not in kws

    def test_skip_existing_preserves_title(self, tmp_path):
        img = make_jpeg(tmp_path / "p.jpg")
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-IPTC:ObjectName=Original Title", str(img)],
                         capture_output=True, text=True)
        meta = PhotoMetadata(title="AI Title", caption="C", keywords=[])
        MetadataWriter.write_metadata(str(img), meta, backup=False,
                                      skip_existing=True)
        title, _, _ = MetadataWriter.read_existing_metadata(str(img))
        assert title == "Original Title"


@needs_exiftool
class TestWriteSidecar:
    def test_numeric_existing_keyword_sidecar_regression(self, tmp_path):
        # The reported .ORF case: numeric keyword in the sidecar, append on
        raw = tmp_path / "shot.orf"
        raw.write_bytes(b"\x00" * 64)
        sidecar = tmp_path / "shot.orf.xmp"
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-XMP-dc:Subject=2025", "-XMP-dc:Subject=beach",
                          str(sidecar)], capture_output=True, text=True)
        meta = PhotoMetadata(title="T", caption="C", keywords=["ocean"])
        ok = MetadataWriter._write_sidecar(raw, meta, adobe_naming=False,
                                           append_keywords=True)
        assert ok is True
        _, _, kws = MetadataWriter.read_existing_metadata(str(sidecar))
        low = [k.lower() for k in kws]
        assert "2025" in low and "beach" in low and "ocean" in low


@needs_exiftool
class TestBatchWriteWorker:
    """Drive MetadataWriteWorker.run() (the -stay_open batch path) directly.

    run() executes synchronously here (we call it, not start()), so no event
    loop is needed — we just assert the on-disk result. The `qapp` fixture
    provides a QApplication for QThread/Signal machinery.
    """
    @pytest.fixture(autouse=True)
    def _use_qapp(self, qapp):
        pass

    def _seed(self, path, args):
        make_jpeg(path)
        photoscribe._run(
            [MetadataWriter.find_exiftool(), "-overwrite_original", *args, str(path)],
            capture_output=True, text=True)
        return path

    def _kws(self, path):
        _, _, k = MetadataWriter.read_existing_metadata(str(path))
        return sorted(k)

    def test_replace_clears_old_keywords(self, tmp_path):
        # Regression for the PR #16 fix: replace mode must clear existing
        # keywords, not accumulate them (the += bug left old ones behind).
        f = self._seed(tmp_path / "r.jpg",
                       ["-IPTC:Keywords=old1", "-IPTC:Keywords=old2"])
        MetadataWriteWorker(
            [(str(f), PhotoMetadata(title="T", caption="C",
                                    keywords=["new1", "new2"]))],
            backup=False, append_keywords=False).run()
        assert self._kws(f) == ["new1", "new2"]

    def test_append_preserves_numeric_existing(self, tmp_path):
        f = self._seed(tmp_path / "a.jpg",
                       ["-IPTC:Keywords=2025", "-IPTC:Keywords=beach"])
        MetadataWriteWorker(
            [(str(f), PhotoMetadata(title="T", caption="C",
                                    keywords=["ocean", "beach"]))],
            backup=False, append_keywords=True).run()
        assert self._kws(f) == ["2025", "beach", "ocean"]

    def test_skip_existing_preserves_title(self, tmp_path):
        f = self._seed(tmp_path / "s.jpg", ["-IPTC:ObjectName=Keep Me"])
        MetadataWriteWorker(
            [(str(f), PhotoMetadata(title="AI", caption="C", keywords=["k"]))],
            backup=False, append_keywords=True, skip_existing=True).run()
        title, _, _ = MetadataWriter.read_existing_metadata(str(f))
        assert title == "Keep Me"


# ── lr:HierarchicalSubject for darktable (v1.6.2) ─────────────────

@needs_exiftool
class TestHierarchicalSubject:
    """darktable reads keywords from lr:hierarchicalSubject and ignores
    dc:subject, so keywords written only to dc:subject never showed up."""

    def _hs(self, path):
        exiftool = MetadataWriter.find_exiftool()
        r = photoscribe._run([exiftool, "-s3", "-XMP-lr:HierarchicalSubject", str(path)],
                             capture_output=True, text=True)
        return [s.strip() for s in r.stdout.strip().split(",") if s.strip()]

    def test_embedded_replace_writes_hierarchical(self, tmp_path):
        img = make_jpeg(tmp_path / "a.jpg")
        MetadataWriter.write_metadata(
            str(img), PhotoMetadata(title="T", caption="C",
                                    keywords=["Gerroa", "beach"]), backup=False)
        assert self._hs(img) == ["Gerroa", "beach"]

    def test_embedded_append_writes_hierarchical(self, tmp_path):
        img = make_jpeg(tmp_path / "b.jpg")
        MetadataWriter.write_metadata(
            str(img), PhotoMetadata(title="T", caption="C", keywords=["x"]),
            backup=False, append_keywords=True)
        assert "x" in self._hs(img)

    def test_sidecar_writes_hierarchical(self, tmp_path):
        raw = tmp_path / "c.cr2"
        raw.write_bytes(b"\x00" * 32)
        MetadataWriter._write_sidecar(
            raw, PhotoMetadata(title="T", caption="C", keywords=["Gerroa", "surf"]),
            adobe_naming=True)
        assert self._hs(tmp_path / "c.xmp") == ["Gerroa", "surf"]

    def test_batch_writes_hierarchical(self, tmp_path):
        img = make_jpeg(tmp_path / "d.jpg")
        n, errs = MetadataWriter.write_metadata_batch(
            [(str(img), PhotoMetadata(title="T", caption="C", keywords=["k1", "k2"]))],
            backup=False)
        assert n == 1 and not errs
        assert self._hs(img) == ["k1", "k2"]

    def test_replace_clears_old_hierarchical(self, tmp_path):
        img = make_jpeg(tmp_path / "e.jpg")
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-XMP-lr:HierarchicalSubject=stale", str(img)],
                         capture_output=True, text=True)
        MetadataWriter.write_metadata(
            str(img), PhotoMetadata(title="T", caption="C", keywords=["fresh"]),
            backup=False)
        assert self._hs(img) == ["fresh"], "old hierarchical keyword survived replace"

    def test_reads_darktable_hierarchical_tags(self, tmp_path):
        # darktable writes a hierarchy; the leaf is what belongs in the prompt.
        img = make_jpeg(tmp_path / "f.jpg")
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-XMP-lr:HierarchicalSubject=Places|Australia|Gerroa",
                          "-XMP-lr:HierarchicalSubject=wildlife", str(img)],
                         capture_output=True, text=True)
        tags = MetadataWriter.read_keywords(str(img))
        assert "Gerroa" in tags and "wildlife" in tags
        assert not any("|" in t for t in tags)


# ── Sidecars must not be duplicated or strip ratings (v1.6.1) ─────

@needs_exiftool
class TestSidecarRatingsAndNaming:
    """Reported by a viewer: star ratings vanished after reading metadata back
    into Lightroom. PhotoScribe never strips a rating — but it could write a
    SECOND sidecar under the other naming convention, stranding the rated one."""

    def _rating(self, path):
        exiftool = MetadataWriter.find_exiftool()
        r = photoscribe._run([exiftool, "-s3", "-XMP:Rating", "-XMP:Label", str(path)],
                             capture_output=True, text=True)
        return r.stdout.strip().replace("\n", "/")

    def _seed_sidecar(self, path, rating=5):
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original", f"-XMP:Rating={rating}",
                          "-XMP:Label=Blue", str(path)], capture_output=True, text=True)

    def test_existing_sidecar_keeps_its_rating(self, tmp_path):
        raw = tmp_path / "a.cr2"
        raw.write_bytes(b"\x00" * 32)
        side = tmp_path / "a.xmp"
        self._seed_sidecar(side)
        MetadataWriter._write_sidecar(
            raw, PhotoMetadata(title="T", caption="C", keywords=["x"]),
            adobe_naming=True)
        assert self._rating(side) == "5/Blue"

    def test_no_duplicate_sidecar_across_naming_conventions(self, tmp_path):
        # Lightroom wrote a.xmp; the DAM setting says a.cr2.xmp. We must reuse
        # Lightroom's rather than orphan it behind a second file.
        raw = tmp_path / "a.cr2"
        raw.write_bytes(b"\x00" * 32)
        lrc_side = tmp_path / "a.xmp"
        self._seed_sidecar(lrc_side)
        MetadataWriter._write_sidecar(
            raw, PhotoMetadata(title="NewTitle", caption="C", keywords=["x"]),
            adobe_naming=False)
        sidecars = sorted(p.name for p in tmp_path.glob("*.xmp"))
        assert sidecars == ["a.xmp"], f"duplicate sidecar created: {sidecars}"
        assert self._rating(lrc_side) == "5/Blue"
        title, _, _ = MetadataWriter.read_existing_metadata(str(lrc_side))
        assert title == "NewTitle"

    def test_reverse_naming_direction(self, tmp_path):
        raw = tmp_path / "b.cr2"
        raw.write_bytes(b"\x00" * 32)
        side = tmp_path / "b.cr2.xmp"
        self._seed_sidecar(side, rating=2)
        MetadataWriter._write_sidecar(
            raw, PhotoMetadata(title="T2", caption="C", keywords=["x"]),
            adobe_naming=True)
        assert sorted(p.name for p in tmp_path.glob("*.xmp")) == ["b.cr2.xmp"]
        assert self._rating(side).startswith("2")

    def test_embedded_write_keeps_rating(self, tmp_path):
        img = make_jpeg(tmp_path / "r.jpg")
        self._seed_sidecar(img, rating=4)
        MetadataWriter.write_metadata(
            str(img), PhotoMetadata(title="T", caption="C", keywords=["a"]),
            backup=False)
        assert self._rating(img) == "4/Blue"


# ── UTF-8 accented keywords (v1.5.4) ──────────────────────────────

@needs_exiftool
class TestAccentedKeywords:
    """Accented keywords used to be written as Latin-1 and shown as "?" in
    Lightroom. v1.5.4 marks the IPTC block UTF-8 (CodedCharacterSet=UTF8)."""

    def _ccs(self, path):
        exiftool = MetadataWriter.find_exiftool()
        r = photoscribe._run([exiftool, "-s3", "-IPTC:CodedCharacterSet", str(path)],
                             capture_output=True, text=True)
        return r.stdout.strip()

    def test_single_write_preserves_accents(self, tmp_path):
        img = make_jpeg(tmp_path / "a.jpg")
        meta = PhotoMetadata(title="T", caption="C",
                             keywords=["Château de Chenonceau", "Provençe", "Nîmes"])
        MetadataWriter.write_metadata(str(img), meta, backup=False)
        _, _, kws = MetadataWriter.read_existing_metadata(str(img))
        assert "Château de Chenonceau" in kws
        assert "Nîmes" in kws
        assert self._ccs(img) == "UTF8"

    def test_batch_write_preserves_accents(self, tmp_path):
        img = make_jpeg(tmp_path / "b.jpg")
        meta = PhotoMetadata(title="Café", caption="C",
                             keywords=["Château de Chenonceau", "Loire Valley"])
        n, errs = MetadataWriter.write_metadata_batch(
            [(str(img), meta)], backup=False, append_keywords=False)
        assert n == 1 and not errs
        _, _, kws = MetadataWriter.read_existing_metadata(str(img))
        assert "Château de Chenonceau" in kws
        assert self._ccs(img) == "UTF8"


# ── GPS + location from XMP sidecars (v1.5.4) ─────────────────────

@needs_exiftool
class TestSidecarLocation:
    """RAW files geotagged in Lightroom / Geotag Photos Pro keep GPS and the
    resolved City/State/Country in a .xmp sidecar, not baked into the raw.
    v1.5.4 reads the sidecar so the location reaches the prompt."""

    def _make_raw_with_sidecar(self, tmp_path, adobe_naming=False):
        raw = tmp_path / "DSCF1234.RAF"
        raw.write_bytes(b"\x00" * 32)  # dummy raw exiftool can't read
        sidecar = (raw.with_suffix(".xmp") if adobe_naming
                   else Path(str(raw) + ".xmp"))
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-XMP:GPSLatitude=43.383686",
                          "-XMP:GPSLongitude=-4.292961",
                          "-XMP-photoshop:City=Comillas",
                          "-XMP-photoshop:State=Cantabria",
                          "-XMP-photoshop:Country=Spain",
                          str(sidecar)], capture_output=True, text=True)
        return raw

    def test_gps_read_from_sidecar(self, tmp_path):
        raw = self._make_raw_with_sidecar(tmp_path)
        coords = photoscribe.read_gps_coordinates(str(raw))
        assert coords is not None
        assert abs(coords[0] - 43.383686) < 1e-4
        assert abs(coords[1] - (-4.292961)) < 1e-4

    def test_location_fields_read_from_sidecar(self, tmp_path):
        raw = self._make_raw_with_sidecar(tmp_path)
        loc = photoscribe.read_location_fields(str(raw))
        assert loc is not None
        assert "Comillas" in loc and "Spain" in loc
        # City before Country in the assembled string
        assert loc.index("Comillas") < loc.index("Spain")

    def test_location_fields_adobe_naming(self, tmp_path):
        raw = self._make_raw_with_sidecar(tmp_path, adobe_naming=True)
        loc = photoscribe.read_location_fields(str(raw))
        assert loc and "Comillas" in loc

    def test_no_sidecar_returns_none(self, tmp_path):
        raw = tmp_path / "bare.RAF"
        raw.write_bytes(b"\x00" * 32)
        assert photoscribe.read_gps_coordinates(str(raw)) is None
        assert photoscribe.read_location_fields(str(raw)) is None


# ── Per-photo location at generation time (issue #18 follow-up) ───

class TestPerPhotoLocation:
    """Location is resolved per photo when the prompt is built, not once at
    load into a shared field. So enabling the option after loading works,
    Regenerate picks it up, and a mixed-location folder tags each photo."""

    def test_location_injected_per_photo(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        monkeypatch.setattr(photoscribe, "resolve_photo_location",
                            lambda fp, coords=None: "Gerroa, New South Wales, Australia")
        w = make_worker(describe_people=False, gps_lookup=True)
        photo = PhotoItem(filepath="x.cr2", filename="x.cr2")
        prompt = w._build_prompt(photo)
        assert "Gerroa, New South Wales, Australia" in prompt
        assert "ground truth" in prompt.lower()

    def test_no_lookup_when_disabled(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        called = []
        monkeypatch.setattr(photoscribe, "resolve_photo_location",
                            lambda fp, coords=None: called.append(fp) or "Gerroa")
        w = make_worker(describe_people=False, gps_lookup=False)
        prompt = w._build_prompt(PhotoItem(filepath="x.cr2", filename="x.cr2"))
        assert "Gerroa" not in prompt
        assert called == []  # must not hit exiftool/network when off

    def test_manual_location_overrides(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        monkeypatch.setattr(photoscribe, "resolve_photo_location",
                            lambda fp, coords=None: "Gerroa")
        w = make_worker(describe_people=False, gps_lookup=True,
                        context="Location: Berry, NSW", has_manual_location=True)
        prompt = w._build_prompt(PhotoItem(filepath="x.cr2", filename="x.cr2"))
        assert "Berry, NSW" in prompt
        assert "Gerroa" not in prompt

    def test_different_photos_get_different_locations(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        places = {"a.cr2": "Gerroa, Australia", "b.cr2": "Comillas, Spain"}
        monkeypatch.setattr(photoscribe, "resolve_photo_location",
                            lambda fp, coords=None: places[fp])
        w = make_worker(describe_people=False, gps_lookup=True)
        pa = w._build_prompt(PhotoItem(filepath="a.cr2", filename="a.cr2"))
        pb = w._build_prompt(PhotoItem(filepath="b.cr2", filename="b.cr2"))
        assert "Gerroa, Australia" in pa and "Comillas" not in pa
        assert "Comillas, Spain" in pb and "Gerroa" not in pb


# ── Per-photo capture date (v1.6.3) ───────────────────────────────

class TestPerPhotoDate:
    """The capture date used to be read from the first photo and applied to
    the whole batch, so a folder spanning a day labelled every frame with the
    first one's timestamp. Reported by a viewer on YouTube."""

    def test_date_injected_per_photo(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        monkeypatch.setattr(photoscribe, "resolve_photo_date",
                            lambda fp: "24 October 2015, 5:57 pm")
        w = make_worker(describe_people=False, exif_date=True)
        prompt = w._build_prompt(PhotoItem(filepath="x.cr2", filename="x.cr2"))
        assert "24 October 2015, 5:57 pm" in prompt
        assert "sunrise or sunset" in prompt

    def test_different_photos_get_different_dates(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        dates = {"a.cr2": "1 January 2015, 6:02 am",
                 "b.cr2": "1 January 2015, 8:14 pm"}
        monkeypatch.setattr(photoscribe, "resolve_photo_date", lambda fp: dates[fp])
        w = make_worker(describe_people=False, exif_date=True)
        pa = w._build_prompt(PhotoItem(filepath="a.cr2", filename="a.cr2"))
        pb = w._build_prompt(PhotoItem(filepath="b.cr2", filename="b.cr2"))
        assert "6:02 am" in pa and "8:14 pm" not in pa
        assert "8:14 pm" in pb and "6:02 am" not in pb

    def test_no_date_when_disabled(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        called = []
        monkeypatch.setattr(photoscribe, "resolve_photo_date",
                            lambda fp: called.append(fp) or "1 January 2015")
        w = make_worker(describe_people=False, exif_date=False)
        prompt = w._build_prompt(PhotoItem(filepath="x.cr2", filename="x.cr2"))
        assert "1 January 2015" not in prompt
        assert called == []

    def test_manual_date_overrides(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        monkeypatch.setattr(photoscribe, "resolve_photo_date",
                            lambda fp: "24 October 2015")
        w = make_worker(describe_people=False, exif_date=True,
                        context="Date/Time: Spring 2019", has_manual_date=True)
        prompt = w._build_prompt(PhotoItem(filepath="x.cr2", filename="x.cr2"))
        assert "Spring 2019" in prompt
        assert "24 October 2015" not in prompt


@needs_exiftool
class TestExifDateWithTime:
    def test_time_included_when_requested(self, tmp_path):
        img = make_jpeg(tmp_path / "t.jpg")
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-DateTimeOriginal=2015:10:24 17:57:43", str(img)],
                         capture_output=True, text=True)
        assert photoscribe.read_exif_date(str(img)) == "24 October 2015"
        with_time = photoscribe.read_exif_date(str(img), with_time=True)
        assert with_time.startswith("24 October 2015")
        assert "5:57" in with_time and "pm" in with_time

    def test_date_read_from_sidecar(self, tmp_path):
        raw = tmp_path / "s.cr2"
        raw.write_bytes(b"\x00" * 32)
        sidecar = tmp_path / "s.xmp"
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          "-XMP:DateTimeOriginal=2015:10:24 06:02:00", str(sidecar)],
                         capture_output=True, text=True)
        assert photoscribe.read_exif_date(str(raw)) == "24 October 2015"


# ── Geocoder place-name priority + caching ────────────────────────

class TestReverseGeocode:
    def _resp(self, address):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self_inner): return {"address": address,
                                          "display_name": "x, y, z"}
        return R()

    def test_locality_is_used(self, monkeypatch):
        # Gerroa (rural NSW) comes back only under "locality"; dropping it
        # left the useless "New South Wales, Australia".
        photoscribe._geocode_cache.clear()
        monkeypatch.setattr(photoscribe.requests, "get",
                            lambda *a, **k: self._resp(
                                {"locality": "Gerroa", "state": "New South Wales",
                                 "country": "Australia"}))
        assert photoscribe.reverse_geocode(-34.7728, 150.8148) == \
            "Gerroa, New South Wales, Australia"

    def test_neighbourhood_is_used(self, monkeypatch):
        photoscribe._geocode_cache.clear()
        monkeypatch.setattr(photoscribe.requests, "get",
                            lambda *a, **k: self._resp(
                                {"neighbourhood": "The Rocks", "state": "NSW",
                                 "country": "Australia"}))
        assert photoscribe.reverse_geocode(-33.86, 151.21).startswith("The Rocks")

    def test_result_is_cached(self, monkeypatch):
        photoscribe._geocode_cache.clear()
        calls = []
        def fake_get(*a, **k):
            calls.append(1)
            return self._resp({"locality": "Gerroa", "country": "Australia"})
        monkeypatch.setattr(photoscribe.requests, "get", fake_get)
        a = photoscribe.reverse_geocode(-34.7728, 150.8148)
        b = photoscribe.reverse_geocode(-34.77281, 150.81479)  # same to 4dp
        assert a == b
        assert len(calls) == 1, "second lookup should hit the cache"


# ── Location lookup independent of folder context (issue #18) ─────

class TestLoadTimeAutofill:
    """v1.5.5 / issue #18: the autofill method used to bail out early unless
    'Use folder context' was ticked, taking the EXIF-date fallback down with
    it. Each source now runs on its own checkbox. The location is no longer
    filled here at all — it is resolved per photo at generation time."""

    def _window(self, monkeypatch):
        monkeypatch.setattr(photoscribe, "read_exif_date",
                            lambda fp, with_time=False: "24 October 2015")
        w = photoscribe.PhotoScribe()
        w.context_fields["ctx_location"].setText("")
        w.context_fields["ctx_datetime"].setText("")
        return w

    def test_date_not_pinned_at_load(self, qapp, monkeypatch):
        # v1.6.3: the first photo's date used to be written into the shared
        # Date/Time field and then applied to every photo in the batch.
        w = self._window(monkeypatch)
        w.folder_context_check.setChecked(False)
        w.exif_date_fallback_check.setChecked(True)
        w._detect_and_apply_folder_context(["/fake/DSCF1234.RAF"])
        assert w.context_fields["ctx_datetime"].text() == ""

    def test_location_not_pinned_at_load(self, qapp, monkeypatch):
        # Would previously pin the whole batch to the first geotagged file.
        w = self._window(monkeypatch)
        monkeypatch.setattr(photoscribe, "read_location_fields",
                            lambda fp: "Comillas, Spain")
        w.gps_lookup_check.blockSignals(True)
        w.gps_lookup_check.setChecked(True)
        w.gps_lookup_check.blockSignals(False)
        w._detect_and_apply_folder_context(["/fake/DSCF1234.RAF"])
        assert w.context_fields["ctx_location"].text() == ""

    def test_clear_all_clears_autofilled_context(self, qapp, monkeypatch):
        # v1.6.3: auto-detected context survived "Clear All" and carried over
        # into the next folder loaded.
        w = self._window(monkeypatch)
        w.folder_context_check.setChecked(True)
        w._apply_folder_context(photoscribe.FolderContext(
            date_str="24 October 2015", location="Berry",
            raw_folder="2015-10-24 Berry", subfolder=""))
        assert w.context_fields["ctx_location"].text() == "Berry"
        w._clear_all()
        assert w.context_fields["ctx_location"].text() == ""
        assert w.context_fields["ctx_datetime"].text() == ""

    def test_clear_all_keeps_typed_context(self, qapp, monkeypatch):
        # What the user typed is theirs — clearing the photo list must not
        # throw away an Event or Photographer they set for the session.
        w = self._window(monkeypatch)
        w.context_fields["ctx_event"].setText("Berry Show 2026")
        w.context_fields["ctx_location"].setText("Kiama")
        w._clear_all()
        assert w.context_fields["ctx_event"].text() == "Berry Show 2026"
        assert w.context_fields["ctx_location"].text() == "Kiama"

    def test_gps_lookup_setting_persists(self, qapp, monkeypatch, tmp_path):
        # v1.5.6: the ticked box used to reset on every launch, because it was
        # never written to (or read from) QSettings.
        from PySide6.QtCore import QSettings
        w = self._window(monkeypatch)
        w.settings = QSettings(str(tmp_path / "prefs.ini"),
                               QSettings.IniFormat)  # never touch real prefs
        w.gps_lookup_check.blockSignals(True)
        w.gps_lookup_check.setChecked(True)
        w.gps_lookup_check.blockSignals(False)
        w._save_settings()
        assert w.settings.value("gps_lookup") == "true"
        w.gps_lookup_check.blockSignals(True)
        w.gps_lookup_check.setChecked(False)
        w.gps_lookup_check.blockSignals(False)
        w._load_settings()
        assert w.gps_lookup_check.isChecked() is True


# ── Output styles and social posts (v1.7.0) ───────────────────────

class TestOutputPrompt:
    """File caption styles change what's written INTO the photo; posts are
    extra ready-to-paste text that must never reach the file."""

    def _prompt(self, monkeypatch, **kw):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        w = make_worker(describe_people=False, **kw)
        return w, w._build_prompt(PhotoItem(filepath="x.jpg", filename="x.jpg"))

    def test_catalogue_adds_nothing(self, monkeypatch):
        _, prompt = self._prompt(monkeypatch, file_style="catalogue")
        assert "stock photography" not in prompt
        assert "Flickr" not in prompt
        assert '"posts"' not in prompt

    def test_stock_style(self, monkeypatch):
        _, prompt = self._prompt(monkeypatch, file_style="stock")
        assert "stock photography" in prompt
        assert "25 to 45 keywords" in prompt

    def test_flickr_style(self, monkeypatch):
        _, prompt = self._prompt(monkeypatch, file_style="flickr")
        assert "Flickr" in prompt

    def test_unknown_style_falls_back_to_catalogue(self, monkeypatch):
        w, _ = self._prompt(monkeypatch, file_style="myspace")
        assert w.file_style == "catalogue"

    def test_posts_requested_in_prompt(self, monkeypatch):
        _, prompt = self._prompt(monkeypatch,
                                 post_targets=["instagram", "alt_text"])
        assert '"instagram":' in prompt and '"alt_text":' in prompt
        assert '"facebook"' not in prompt
        # The format example carries the posts object too.
        assert '"posts": {"instagram": "...", "alt_text": "..."}' in prompt
        assert "never put hashtags or emoji in the title" in prompt

    def test_post_order_is_canonical_and_unknowns_dropped(self, monkeypatch):
        w, _ = self._prompt(monkeypatch,
                            post_targets=["alt_text", "tiktok", "instagram"])
        assert w.post_targets == ["instagram", "alt_text"]

    def test_first_person_voice(self, monkeypatch):
        _, prompt = self._prompt(monkeypatch, post_targets=["instagram"],
                                 first_person=True)
        assert "in the first person" in prompt

    def test_neutral_voice_by_default(self, monkeypatch):
        _, prompt = self._prompt(monkeypatch, post_targets=["instagram"])
        assert "neutral voice" in prompt
        assert "in the first person" not in prompt

    def test_schema_unchanged_without_posts(self):
        w = make_worker()
        assert w._schema() is OllamaWorker._JSON_SCHEMA

    def test_schema_requires_each_post(self):
        w = make_worker(post_targets=["mastodon", "instagram"])
        sch = w._schema()["schema"]
        assert sch["required"] == ["title", "caption", "keywords", "posts"]
        assert sch["properties"]["posts"]["required"] == ["instagram", "mastodon"]
        # The shared base schema must not have been mutated.
        assert "posts" not in OllamaWorker._JSON_SCHEMA["schema"]["properties"]

    def test_openai_request_carries_posts_schema(self, monkeypatch):
        seen = []
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: (seen.append(json), _Resp())[1])
        make_worker(backend="openai", post_targets=["facebook"])._call_openai("img", "p")
        props = seen[0]["response_format"]["json_schema"]["schema"]["properties"]
        assert "facebook" in props["posts"]["properties"]


class TestPostsInResults:
    def _run(self, monkeypatch, reply, **kw):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        photo = PhotoItem(filepath="x.jpg", filename="x.jpg")
        w = make_worker(photos=[photo], backend="openai", **kw)
        monkeypatch.setattr(w, "_encode_image", lambda fp: "img")
        monkeypatch.setattr(w, "_call_openai", lambda img, prompt: reply)
        results, logs = [], []
        w.result.connect(lambda i, r: results.append(r))
        w.log_message.connect(logs.append)
        w.run()
        return results[0], logs

    def test_posts_reach_the_result(self, qapp, monkeypatch):
        reply = json.dumps({"title": "T", "caption": "C", "keywords": ["a"],
                            "posts": {"instagram": " Golden hour #Gerroa ",
                                      "alt_text": "A beach at sunset"}})
        meta, _ = self._run(monkeypatch, reply,
                            post_targets=["instagram", "alt_text"])
        assert meta.posts == {"instagram": "Golden hour #Gerroa",
                              "alt_text": "A beach at sunset"}

    def test_unrequested_posts_are_ignored(self, qapp, monkeypatch):
        reply = json.dumps({"title": "T", "caption": "C", "keywords": ["a"],
                            "posts": {"instagram": "x", "facebook": "y"}})
        meta, _ = self._run(monkeypatch, reply, post_targets=["instagram"])
        assert meta.posts == {"instagram": "x"}

    def test_missing_post_is_logged(self, qapp, monkeypatch):
        reply = json.dumps({"title": "T", "caption": "C", "keywords": ["a"],
                            "posts": {"instagram": "x"}})
        meta, logs = self._run(monkeypatch, reply,
                               post_targets=["instagram", "mastodon"])
        assert "mastodon" not in meta.posts
        assert any("No post returned for: Mastodon" in m for m in logs)


@needs_exiftool
class TestPostsNeverWrittenToFile:
    def test_post_text_absent_from_every_tag(self, tmp_path):
        img = make_jpeg(tmp_path / "p.jpg")
        meta = PhotoMetadata(title="T", caption="C", keywords=["k"],
                             posts={"instagram": "ZZPOSTMARKER #nope"})
        MetadataWriter.write_metadata(str(img), meta, backup=False)
        MetadataWriter.write_metadata_batch([(str(img), meta)], backup=False)
        exiftool = MetadataWriter.find_exiftool()
        r = photoscribe._run([exiftool, "-a", "-G1", "-j", str(img)],
                             capture_output=True, text=True)
        assert "ZZPOSTMARKER" not in r.stdout


class TestOutputsUI:
    def _window(self, monkeypatch, tmp_path):
        from PySide6.QtCore import QSettings
        w = photoscribe.PhotoScribe()
        w.settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
        return w

    def _done_photo(self, path, posts):
        p = PhotoItem(filepath=str(path), filename=path.name)
        p.status = "done"
        p.metadata = PhotoMetadata(title="T", caption="C", keywords=["k"],
                                   posts=dict(posts))
        return p

    def test_settings_persist(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        w.file_style_combo.setCurrentIndex(w.file_style_combo.findData("stock"))
        w.post_checks["instagram"].setChecked(True)
        w.post_checks["alt_text"].setChecked(True)
        w.first_person_check.setChecked(True)
        w._save_settings()
        for cb in w.post_checks.values():
            cb.setChecked(False)
        w.file_style_combo.setCurrentIndex(0)
        w.first_person_check.setChecked(False)
        w._load_settings()
        assert w.file_style_combo.currentData() == "stock"
        assert w._selected_post_targets() == ["instagram", "alt_text"]
        assert w.first_person_check.isChecked()

    def test_first_person_disabled_without_posts(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        for cb in w.post_checks.values():
            cb.setChecked(False)
        assert not w.first_person_check.isEnabled()
        w.post_checks["facebook"].setChecked(True)
        assert w.first_person_check.isEnabled()

    def test_only_boxes_with_posts_are_shown(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        photo = self._done_photo(tmp_path / "a.jpg", {"instagram": "Hello #x"})
        monkeypatch.setattr(w, "_load_preview", lambda fp: None)
        w._load_detail(photo)
        assert not w.post_boxes["instagram"][0].isHidden()
        assert w.post_boxes["facebook"][0].isHidden()
        assert not w.posts_header.isHidden()
        assert w.post_boxes["instagram"][1].toPlainText() == "Hello #x"

    def test_char_count_turns_red_over_limit(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        edit, count = w.post_boxes["threads_bluesky"][1], w.post_boxes["threads_bluesky"][2]
        edit.setPlainText("x" * 301)
        assert count.text() == "301 / 300"
        assert "#e2796a" in count.styleSheet()

    def test_copy_button_copies(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QApplication
        w = self._window(monkeypatch, tmp_path)
        _, edit, _, btn = w.post_boxes["mastodon"]
        edit.setPlainText("Surf at dusk #GoldenHour")
        btn.click()
        assert QApplication.clipboard().text() == "Surf at dusk #GoldenHour"

    def test_post_edits_sync_back(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        photo = self._done_photo(tmp_path / "a.jpg", {"instagram": "old"})
        w.photos = [photo]
        monkeypatch.setattr(w, "_load_preview", lambda fp: None)
        w._current_result_index = 0
        w._load_detail(photo)
        # The Results tab isn't on screen here, which is the case that used
        # to drop edits when the check was isVisible().
        w.post_boxes["instagram"][1].setPlainText("edited")
        assert photo.metadata.posts["instagram"] == "edited"

    def test_progress_round_trip_keeps_posts(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        img = tmp_path / "a.jpg"
        w.photos = [self._done_photo(img, {"alt_text": "A bench"})]
        w._save_progress()
        w.photos = [PhotoItem(filepath=str(img), filename="a.jpg")]
        assert w._load_progress([str(img)]) == 1
        assert w.photos[0].metadata.posts == {"alt_text": "A bench"}

    def test_csv_round_trip_keeps_posts(self, qapp, monkeypatch, tmp_path):
        w = self._window(monkeypatch, tmp_path)
        img = tmp_path / "a.jpg"
        w.photos = [self._done_photo(img, {"instagram": "Hi #x", "alt_text": "A"})]
        csv_path = str(tmp_path / "out.csv")
        monkeypatch.setattr(photoscribe.QFileDialog, "getSaveFileName",
                            lambda *a, **k: (csv_path, ""))
        monkeypatch.setattr(photoscribe.QFileDialog, "getOpenFileName",
                            lambda *a, **k: (csv_path, ""))
        monkeypatch.setattr(photoscribe.QMessageBox, "information",
                            lambda *a, **k: None)
        w._export_csv()
        header = open(csv_path, encoding="utf-8").readline()
        assert "Post: Instagram" in header and "Post: Alt text" in header
        assert "Post: Facebook" not in header
        w.photos = [PhotoItem(filepath=str(img), filename="a.jpg")]
        w._import_csv()
        assert w.photos[0].metadata.posts == {"instagram": "Hi #x", "alt_text": "A"}


# ── Posts on demand, slow-model warning, recommender (v1.7.0) ─────

class TestPostsOnDemand:
    """Results → Write posts writes posts for one finished photo, leaving
    its title, caption and keywords alone."""

    def _done(self, path="x.jpg"):
        p = PhotoItem(filepath=path, filename=path)
        p.status = "done"
        p.metadata = PhotoMetadata(title="Bench at Gerringong", caption="A bench.",
                                   keywords=["bench"], posts={"facebook": "keep me"})
        return p

    def test_posts_only_prompt_skips_metadata_request(self, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        w = make_worker(describe_people=False, post_targets=["instagram"],
                        posts_only=True, file_style="stock",
                        keywords_list=["seascape"])
        prompt = w._build_prompt(self._done())
        assert 'Title: "Bench at Gerringong"' in prompt       # keeps it consistent
        assert "Describe the photo." not in prompt             # user prompt skipped
        assert "stock photography" not in prompt               # file style skipped
        assert "seascape" not in prompt                        # vocab is for keywords
        assert '{"posts": {"instagram": "..."}}' in prompt

    def test_posts_only_schema_is_just_posts(self):
        w = make_worker(post_targets=["alt_text"], posts_only=True)
        sch = w._schema()["schema"]
        assert sch["required"] == ["posts"]
        assert set(sch["properties"]) == {"posts"}

    def test_posts_only_needs_a_target(self):
        assert make_worker(post_targets=[], posts_only=True).posts_only is False

    def test_run_processes_a_done_photo_and_returns_only_posts(self, qapp, monkeypatch):
        monkeypatch.setattr(MetadataWriter, "read_persons", lambda f: [])
        monkeypatch.setattr(MetadataWriter, "read_keywords", lambda f: [])
        photo = self._done()
        w = make_worker(photos=[photo], backend="openai",
                        post_targets=["instagram"], posts_only=True)
        monkeypatch.setattr(w, "_encode_image", lambda fp: "img")
        monkeypatch.setattr(w, "_call_openai", lambda img, prompt:
                            json.dumps({"posts": {"instagram": "Hi #x"}}))
        out = []
        w.result.connect(lambda i, r: out.append(r))
        w.run()
        assert len(out) == 1 and out[0].posts == {"instagram": "Hi #x"}
        assert out[0].title == ""            # nothing but posts comes back

    def test_ui_merges_posts_and_keeps_metadata(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtCore import QSettings
        w = photoscribe.PhotoScribe()
        w.settings = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)
        photo = self._done(str(tmp_path / "a.jpg"))
        w.photos = [photo]
        w._current_result_index = 0
        monkeypatch.setattr(w, "_load_preview", lambda fp: None)
        w._posts_photo = photo
        w._on_posts_result(0, PhotoMetadata(posts={"instagram": "new"}))
        assert photo.metadata.posts == {"facebook": "keep me", "instagram": "new"}
        assert photo.metadata.title == "Bench at Gerringong"
        assert photo.metadata.keywords == ["bench"]

    def test_write_posts_button_state(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtCore import QSettings
        w = photoscribe.PhotoScribe()
        w.settings = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)
        for cb in w.post_checks.values():
            cb.setChecked(False)
        w.photos = [self._done(str(tmp_path / "a.jpg"))]
        w._current_result_index = 0
        monkeypatch.setattr(w, "_load_preview", lambda fp: None)
        w._load_detail(w.photos[0])
        assert not w.write_posts_btn.isEnabled()          # nothing ticked
        w.post_checks["instagram"].setChecked(True)
        assert w.write_posts_btn.isEnabled()
        assert w.write_posts_btn.text() == "Rewrite posts"  # already has one

    def test_batch_skips_posts_unless_asked(self, qapp, monkeypatch, tmp_path):
        from PySide6.QtCore import QSettings
        monkeypatch.setattr(photoscribe.OllamaWorker, "start", lambda self: None)
        w = photoscribe.PhotoScribe()
        w.settings = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)
        w.model_combo.addItem("m")
        w.photos = [PhotoItem(filepath="a.jpg", filename="a.jpg")]
        w.post_checks["instagram"].setChecked(True)
        w.batch_posts_check.setChecked(False)
        w._start_processing()
        assert w.worker.post_targets == []
        w.batch_posts_check.setChecked(True)
        w._start_processing()
        assert w.worker.post_targets == ["instagram"]


class TestSlowModelWarning:
    def _worker(self, monkeypatch):
        monkeypatch.setattr(OllamaWorker, "_slow_warned", False)
        w = make_worker(model="big-model")
        logs = []
        w.log_message.connect(logs.append)
        return w, logs

    def test_warns_when_slow(self, monkeypatch):
        w, logs = self._worker(monkeypatch)
        w._last_rate = (110, 20.0)          # 5.5 tok/s, measured on a 26B-A4B
        w._check_speed()
        assert any("Slow generation: 5.5 tokens a second" in m for m in logs)
        assert any("big-model" in m for m in logs)

    def test_quiet_when_healthy(self, monkeypatch):
        w, logs = self._worker(monkeypatch)
        w._last_rate = (132, 9.4)           # 14 tok/s, a model that fits
        w._check_speed()
        assert logs == []

    def test_short_answers_are_not_judged(self, monkeypatch):
        w, logs = self._worker(monkeypatch)
        w._last_rate = (30, 10.0)           # alt text only: start-up dominates
        w._check_speed()
        assert logs == []

    def test_warns_once_per_session(self, monkeypatch):
        w, logs = self._worker(monkeypatch)
        w._last_rate = (110, 20.0)
        w._check_speed()
        w._check_speed()
        make_worker()._check_speed()
        assert len(logs) == 1

    def test_openai_call_records_rate(self, monkeypatch):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                   "usage": {"completion_tokens": 120}}
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: _Resp(payload=payload))
        w = make_worker(backend="openai")
        w._call_openai("img", "p")
        assert w._last_rate[0] == 120 and w._last_rate[1] >= 0

    def test_ollama_call_uses_reported_timing(self, monkeypatch):
        payload = {"message": {"content": "{}"}, "eval_count": 100,
                   "eval_duration": 5_000_000_000}
        monkeypatch.setattr(photoscribe.requests, "post",
                            lambda url, json=None, timeout=None: _Resp(payload=payload))
        w = make_worker(backend="ollama")
        w._call_ollama("img", "p")
        assert w._last_rate == (100, 5.0)


class TestModelRecommendation:
    def _rec(self, qapp, **info):
        w = photoscribe.PhotoScribe()
        return w._get_model_recommendation(info)

    def test_32gb_mac_gets_12b_not_a_big_model(self, qapp):
        # The case measured: 27B and 26B-A4B both paged to disk on 32GB.
        r = self._rec(qapp, ram_mb=32768, vram_mb=22937, platform="apple_silicon")
        assert r["model"] == "gemma4:12b-it-qat"

    def test_big_mac_gets_moe(self, qapp):
        r = self._rec(qapp, ram_mb=64 * 1024, vram_mb=45000, platform="apple_silicon")
        assert r["model"] == "gemma4:26b-a4b-it-qat"

    def test_12gb_card_gets_12b(self, qapp):
        # Previously sent to the 4B; the 12B is 7.2GB and fits.
        r = self._rec(qapp, ram_mb=96 * 1024, vram_mb=12 * 1024, platform="nvidia")
        assert r["model"] == "gemma4:12b-it-qat"

    def test_6gb_card_gets_e2b(self, qapp):
        r = self._rec(qapp, ram_mb=16 * 1024, vram_mb=6 * 1024, platform="nvidia")
        assert r["model"] == "gemma4:e2b-it-qat"

    def test_no_gemma3_left(self, qapp):
        for ram in (8, 16, 24, 32, 48, 64):
            r = self._rec(qapp, ram_mb=ram * 1024, vram_mb=0, platform="apple_silicon")
            assert "gemma3" not in r["model"] and r["pull"].startswith("ollama pull gemma4:")
