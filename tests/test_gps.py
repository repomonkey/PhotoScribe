"""Tests for GPS writing and GPX track matching.

Pure units for the GPX parser, track matcher and capture-time conversion,
plus ExifTool integration tests (skipped without ExifTool) proving that
coordinates land in files and sidecars, and that a photo's own GPS is
never overwritten.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))
import photoscribe
from photoscribe import (MetadataWriter, MetadataWriteWorker, PhotoMetadata,
                         parse_gpx, match_track, photo_epoch,
                         read_gps_coordinates, read_capture_times, _path_key)

needs_exiftool = pytest.mark.skipif(
    MetadataWriter.find_exiftool() is None, reason="ExifTool not available")


def utc(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()


GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <wpt lat="1" lon="1"><time>2026-03-14T00:00:00Z</time></wpt>
  <trk><trkseg>
    <trkpt lat="-34.7700" lon="150.6900"><time>2026-03-14T21:02:00.123456789Z</time></trkpt>
    <trkpt lat="-34.7800" lon="150.7000"><time>2026-03-14T21:00:00Z</time></trkpt>
    <trkpt lat="-34.9000" lon="150.8000"><time>2026-03-14T23:00:00+00:00</time></trkpt>
    <trkpt lat="-35.0000" lon="150.9000"></trkpt>
  </trkseg></trk>
</gpx>
"""


@pytest.fixture
def track(tmp_path):
    path = tmp_path / "walk.gpx"
    path.write_text(GPX)
    return parse_gpx(str(path))


class TestParseGpx:
    def test_reads_timed_track_points_sorted(self, track):
        # waypoint and the untimed point are dropped; out-of-order points sorted
        assert len(track) == 3
        assert track[0][1:] == (-34.78, 150.70)
        assert track[0][0] == utc(2026, 3, 14, 21, 0)
        assert [p[0] for p in track] == sorted(p[0] for p in track)

    def test_nanosecond_fractions_parse(self, track):
        assert track[1][0] == pytest.approx(utc(2026, 3, 14, 21, 2) + 0.123456)

    def test_unnamespaced_gpx_10(self, tmp_path):
        path = tmp_path / "old.gpx"
        path.write_text('<gpx version="1.0"><trk><trkseg>'
                        '<trkpt lat="10" lon="20"><time>2026-01-01T00:00:00Z</time>'
                        '</trkpt></trkseg></trk></gpx>')
        assert parse_gpx(str(path)) == [(utc(2026, 1, 1, 0, 0), 10.0, 20.0)]


class TestMatchTrack:
    points = [(0.0, 0.0, 0.0), (100.0, 1.0, 2.0), (5000.0, 9.0, 9.0)]

    def test_exact_point(self):
        assert match_track(self.points, 100.0) == (1.0, 2.0)

    def test_interpolates_between_close_points(self):
        assert match_track(self.points, 25.0) == pytest.approx((0.25, 0.5))

    def test_big_gap_snaps_only_when_near(self):
        # 4900 s gap is too long to interpolate across
        assert match_track(self.points, 160.0) == (1.0, 2.0)
        assert match_track(self.points, 2500.0) is None

    def test_outside_track(self):
        assert match_track(self.points, -60.0) == (0.0, 0.0)
        assert match_track(self.points, -1000.0) is None
        assert match_track(self.points, 9000.0) is None

    def test_empty_track(self):
        assert match_track([], 5.0) is None


class TestPhotoEpoch:
    def test_file_offset_used(self):
        assert photo_epoch("2026:03:15 08:00:00", "+11:00") == utc(2026, 3, 14, 21, 0)

    def test_chosen_zone_beats_file_offset(self):
        # camera left on home time: the file's own offset is wrong too
        assert photo_epoch("2026:03:15 08:00:00", "+11:00", 10.0) == utc(2026, 3, 14, 22, 0)

    def test_negative_and_half_hour_offsets(self):
        assert photo_epoch("2026:03:14 12:00:00", "-03:30") == utc(2026, 3, 14, 15, 30)

    def test_falls_back_to_this_computer(self):
        t = photo_epoch("2026:03:14 12:00:00")
        assert t == datetime(2026, 3, 14, 12, 0).timestamp()

    def test_unusable_dates(self):
        assert photo_epoch("") is None
        assert photo_epoch("0000:00:00 00:00:00") is None


def make_jpeg(path, dto=None):
    Image.new("RGB", (32, 32)).save(path)
    if dto:
        exiftool = MetadataWriter.find_exiftool()
        photoscribe._run([exiftool, "-overwrite_original",
                          f"-DateTimeOriginal={dto}", str(path)],
                         capture_output=True, text=True, timeout=15)
    return str(path)


META = PhotoMetadata(title="t", caption="c", keywords=["k"])


@needs_exiftool
class TestGpsWrite:
    def test_writes_southern_and_western_coordinates(self, tmp_path):
        a = make_jpeg(tmp_path / "a.jpg")
        b = make_jpeg(tmp_path / "b.jpg")
        MetadataWriteWorker([(a, META), (b, META)], backup=False,
                            gps_by_file={a: (-34.775, 150.698),
                                         b: (40.7, -74.0)}).run()
        assert read_gps_coordinates(a) == pytest.approx((-34.775, 150.698))
        assert read_gps_coordinates(b) == pytest.approx((40.7, -74.0))

    def test_existing_gps_is_kept(self, tmp_path):
        a = make_jpeg(tmp_path / "a.jpg")
        MetadataWriteWorker([(a, META)], backup=False,
                            gps_by_file={a: (1.0, 2.0)}).run()
        MetadataWriteWorker([(a, META)], backup=False,
                            gps_by_file={a: (50.0, 60.0)}).run()
        assert read_gps_coordinates(a) == pytest.approx((1.0, 2.0))

    def test_no_coords_writes_no_gps(self, tmp_path):
        a = make_jpeg(tmp_path / "a.jpg")
        MetadataWriteWorker([(a, META)], backup=False).run()
        assert read_gps_coordinates(a) is None

    def test_sidecar_gets_gps(self, tmp_path):
        raw = tmp_path / "shot.RAF"
        raw.write_bytes(b"not really a raw")
        xmp = tmp_path / "shot.xmp"
        MetadataWriter._write_sidecar(raw, META, adobe_naming=True,
                                      gps_coords=(-33.86, 151.21))
        assert read_gps_coordinates(str(xmp)) == pytest.approx((-33.86, 151.21))

    def test_capture_times_and_full_match(self, tmp_path, track):
        a = make_jpeg(tmp_path / "a.jpg", "2026:03:15 08:01:00")
        times = read_capture_times([a])
        dto, off = times[_path_key(a)]
        assert dto == "2026:03:15 08:01:00"
        t = photo_epoch(dto, off, 11.0)
        assert match_track(track, t) == pytest.approx((-34.775, 150.695), abs=1e-4)


class TestTrackLocationInPrompt:
    def test_track_coords_used_when_file_has_none(self, monkeypatch):
        monkeypatch.setattr(photoscribe, "read_location_fields", lambda f: None)
        monkeypatch.setattr(photoscribe, "read_gps_coordinates", lambda f: None)
        monkeypatch.setattr(photoscribe, "reverse_geocode",
                            lambda lat, lon: f"place at {lat},{lon}")
        assert photoscribe.resolve_photo_location("x.raf", (-34.7, 150.6)) == \
            "place at -34.7,150.6"
        assert photoscribe.resolve_photo_location("x.raf") is None

    def test_files_own_gps_beats_track(self, monkeypatch):
        monkeypatch.setattr(photoscribe, "read_location_fields", lambda f: None)
        monkeypatch.setattr(photoscribe, "read_gps_coordinates", lambda f: (1.0, 2.0))
        monkeypatch.setattr(photoscribe, "reverse_geocode",
                            lambda lat, lon: f"{lat},{lon}")
        assert photoscribe.resolve_photo_location("x.raf", (9.0, 9.0)) == "1.0,2.0"
