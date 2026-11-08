"""Offline regressions for Kodi-certified v2.1 cloud channel promotion."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build


def fake_source(now):
    """NBC Sports NOW + NBC News NOW + two denied channels, with real-shaped XMLTV."""
    root = ET.Element("tv")
    specs = [
        (build.NBC_SOURCE_ID, "NBC Sports NOW"),
        ("some-nbc-news-id", "NBC News NOW"),
        ("some-abc-id", "ABC News Live"),
        ("some-cbs-id", "CBS News 24/7"),
    ]
    for channel_id, name in specs:
        ch = ET.SubElement(root, "channel", id=channel_id)
        ET.SubElement(ch, "display-name").text = name
        for i in range(2):
            start = now + timedelta(hours=i-1)
            end = start + timedelta(hours=2)
            p = ET.SubElement(root, "programme", {
                "channel": channel_id,
                "start": start.strftime("%Y%m%d%H%M%S +0000"),
                "stop": end.strftime("%Y%m%d%H%M%S +0000"),
            })
            ET.SubElement(p, "title").text = name + " actual schedule"
    return ET.tostring(root, encoding="utf-8")


class ApprovedFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.pinned = build.read_channels(cls.root / "channels.json")
        cls.outcomes = build.read_playback_results(
            cls.root / "playback-results.json", build.PREVIEW_CANDIDATES, cls.pinned
        )
        cls.approved = build.select_approved_channels(cls.pinned, build.PREVIEW_CANDIDATES, cls.outcomes)
        cls.now = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)

    def test_pinned_three_unchanged(self):
        self.assertEqual([x["name"] for x in self.pinned],
                         ["France 24 English", "DW English", "NBC Sports NOW"])
        self.assertEqual(self.approved[:3], self.pinned)
        self.assertEqual(build.create_m3u(self.approved[:3]), build.create_m3u(self.pinned))

    def test_exactly_four_new_kodi_confirmed(self):
        self.assertEqual([x["name"] for x in self.approved[3:]],
                         ["beIN SPORTS XTRA", "Red Bull TV", "NBC News NOW", "Bloomberg Television"])
        self.assertEqual(len(self.approved), 7)

    def test_failed_channels_excluded_from_approved(self):
        playlist = build.create_m3u(self.approved)
        self.assertNotIn("ABC News Live", playlist)
        self.assertNotIn("CBS News 24/7", playlist)
        self.assertIn("beIN SPORTS XTRA", playlist)
        self.assertEqual(playlist.count("#EXTINF"), 7)

    def test_approved_streams_do_not_depend_on_manifest_probe(self):
        self.assertEqual(
            build.select_approved_channels(self.pinned, build.PREVIEW_CANDIDATES, self.outcomes),
            self.approved
        )

    def test_mismatched_url_rejected_even_if_marked_works(self):
        with tempfile.TemporaryDirectory() as d:
            data = json.loads((self.root / "playback-results.json").read_text())
            data["results"][0]["url"] = "https://other.example.com/master.m3u8"
            f = Path(d) / "results.json"
            f.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "Changed URL/name"):
                build.read_playback_results(f, build.PREVIEW_CANDIDATES, self.pinned)

    def test_duplicate_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            data = json.loads((self.root / "playback-results.json").read_text())
            data["results"].append(copy.deepcopy(data["results"][0]))
            f = Path(d) / "results.json"
            f.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                build.read_playback_results(f, build.PREVIEW_CANDIDATES, self.pinned)

    def test_invalid_evidence_outcome_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            data = json.loads((self.root / "playback-results.json").read_text())
            data["results"][0]["result"] = "http-200"
            f = Path(d) / "results.json"
            f.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "Invalid playback result"):
                build.read_playback_results(f, build.PREVIEW_CANDIDATES, self.pinned)

    def test_preview_excludes_failed_kodi_streams_even_with_http_200(self):
        all_pass = [(candidate, "HLS manifest HTTP 200") for candidate in build.PREVIEW_CANDIDATES]
        unseen = build.select_preview_candidates(all_pass, self.approved, self.outcomes)
        self.assertEqual([x["name"] for x in unseen],
                         ["Racing America", "World Channel", "Reuters TV"])
        self.assertFalse({x["name"] for x in unseen} & {"ABC News Live", "CBS News 24/7"})

    def test_guide_only_contains_real_approved_matches(self):
        source = fake_source(self.now)
        base, _ = build.create_guide(source, self.pinned, self.now)
        guide, covered = build.create_preview_guide(source, base, self.approved[3:], self.now)
        ids = {c.get("id") for c in ET.fromstring(guide).findall("channel")}
        self.assertEqual(ids, {"NBCSportsNOW.us@SD", "NBCNewsNOW.us@SD"})
        self.assertEqual(covered, ["NBC Sports NOW", "NBC News NOW"])
        self.assertFalse({"ABCNews.us@Free", "CBSNews.us@Free"} & ids)
        ps = ET.fromstring(guide).findall("programme")
        self.assertTrue(ps)
        self.assertEqual({p.get("channel") for p in ps}, ids)

    def test_missing_optional_schedule_does_not_create_fictitious_data(self):
        source = fake_source(self.now)
        root = ET.fromstring(source)
        for ch in root.findall("channel"):
            if ch.findtext("display-name") == "NBC News NOW":
                root.remove(ch)
        for prog in root.findall("programme"):
            if prog.get("channel") == "some-nbc-news-id":
                root.remove(prog)
        source = ET.tostring(root)
        base, _ = build.create_guide(source, self.pinned, self.now)
        guide, covered = build.create_preview_guide(source, base, self.approved[3:], self.now)
        self.assertEqual(covered, ["NBC Sports NOW"])
        self.assertEqual(len(ET.fromstring(guide).findall("channel")), 1)

    def test_stale_required_nbc_source_fails_closed(self):
        old = self.now - timedelta(days=5)
        with self.assertRaisesRegex(ValueError, "lacks current/upcoming"):
            build.create_guide(fake_source(old), self.pinned, self.now)

    def test_publish_complete_outputs_and_independent_feeds(self):
        source = fake_source(self.now)
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            with mock.patch.object(build, "download_guide", return_value=source), \
                 mock.patch.object(build, "probe_hls", side_effect=lambda ch: (ch, "HLS manifest HTTP 200")), \
                 mock.patch.object(build, "DEST", site), \
                 mock.patch.object(build, "ROOT", Path(directory)):
                build.main()
            names = {p.name for p in site.iterdir()}
            self.assertEqual(names, {
                "channels.m3u", "guide.xml", "index.html", "status.json",
                "approved.m3u", "approved-guide.xml", "approved-status.json",
                "preview.m3u", "preview-guide.xml", "preview-status.json",
            })
            self.assertEqual((site / "channels.m3u").read_text(), build.create_m3u(self.pinned))
            self.assertEqual((site / "approved.m3u").read_text().count("#EXTINF"), 7)
            approved_status = json.loads((site / "approved-status.json").read_text())
            self.assertEqual(approved_status["approved_total_channels"], 7)
            self.assertEqual(approved_status["approved_guide_channel_names"],
                             ["NBC Sports NOW", "NBC News NOW"])
            self.assertEqual(approved_status["kodi_playback_failed_channels"],
                             ["ABC News Live", "CBS News 24/7"])
            preview = (site / "preview.m3u").read_text()
            self.assertEqual(preview.count("#EXTINF"), 10)
            self.assertNotIn("ABC News Live", preview)
            self.assertNotIn("CBS News 24/7", preview)

    def test_invalid_evidence_does_not_overwrite_existing_site(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "site"
            site.mkdir()
            (site / "keep.txt").write_text("last good")
            source = fake_source(self.now)
            bad = Path(directory) / "bad-results.json"
            data = json.loads((self.root / "playback-results.json").read_text())
            data["results"][1]["url"] = "https://unreviewed.example.com/fake.m3u8"
            bad.write_text(json.dumps(data))
            with mock.patch.object(build, "PLAYBACK_FILE", bad), \
                 mock.patch.object(build, "download_guide", return_value=source), \
                 mock.patch.object(build, "DEST", site):
                with self.assertRaisesRegex(ValueError, "Changed URL/name"):
                    build.main()
            self.assertEqual((site / "keep.txt").read_text(), "last good")


if __name__ == "__main__":
    unittest.main()
