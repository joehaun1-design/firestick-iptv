"""Pure/offline v2.2 expansion checks: no live stream or provider calls."""
import copy
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import expansion as e

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)


def setup():
    pinned = [
        dict(name="France 24 English", tvg_id="France24.fr@English", group="News", url="https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_2300.m3u8"),
        dict(name="DW English", tvg_id="DW.de@English", group="News", url="https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/master.m3u8"),
        dict(name="NBC Sports NOW", tvg_id="NBCSportsNOW.us@SD", group="Sports", url="https://d4whmvwm0rdvi.cloudfront.net/10007/99993008/hls/master.m3u8?ads.xumo_channelId=99993008"),
    ]
    tests = [
        ("beIN SPORTS XTRA", "beINSPORTSXTRA.us@SD", "Sports", "https://bein-xtra-bein.amagi.tv/playlist.m3u8", "works"),
        ("Red Bull TV", "RedBullTV.at@HD", "Sports", "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8", "works"),
        ("ABC News Live", "ABCNews.us@Free", "News", "https://abcnews-streams.akamaized.net/hls/live/2023560/abcnewshudson1/master.m3u8", "fails"),
        ("CBS News 24/7", "CBSNews.us@Free", "News", "https://cbsnews.akamaized.net/hls/live/2020607/cbsnlineup_8/master.m3u8", "fails"),
        ("NBC News NOW", "NBCNewsNOW.us@SD", "News", "https://d1bl6tskrpq9ze.cloudfront.net/hls/master.m3u8?ads.xumo_channelId=99984003", "works"),
        ("Bloomberg Television", "BloombergTelevision.us@Free", "News", "https://bloomberg.com/media-manifest/streams/us.m3u8", "works"),
    ]
    evidence = dict(schema_version=1, results=[dict(name=n, tvg_id=tid, url=u, result=status) for n, tid, g, u, status in tests])
    approved = pinned + [dict(name=n, tvg_id=tid, group=g, url=u) for n, tid, g, u, status in tests if status=="works"]
    return pinned, evidence, approved


def xml_for(name_ids):
    root = ET.Element("tv")
    for name, tid in name_ids:
        ch = ET.SubElement(root, "channel", id=tid)
        ET.SubElement(ch, "display-name").text = name
        p = ET.SubElement(root, "programme", channel=tid,
            start=(NOW - timedelta(minutes=20)).strftime("%Y%m%d%H%M%S +0000"),
            stop=(NOW + timedelta(hours=3)).strftime("%Y%m%d%H%M%S +0000"))
        ET.SubElement(p, "title").text = name + " Real Test Programme"
    return ET.tostring(root, encoding="utf-8")


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.pinned, self.evidence, self.approved = setup()
        self.catalog = e.load_catalog(ROOT / "expansion-candidates.json", {x["tvg_id"] for x in self.approved} | {"ABCNews.us@Free", "CBSNews.us@Free"})
        self.base_guide = xml_for([("NBC Sports NOW", "NBCSportsNOW.us@SD"), ("NBC News NOW", "NBCNewsNOW.us@SD")])
        self.approved_text = e.render_m3u(self.approved)

    def test_catalog_16_reviewed_distinct_channels(self):
        self.assertEqual(len(self.catalog), 16)
        self.assertEqual(len({c["tvg_id"] for c in self.catalog}), 16)
        self.assertEqual(set(c["group"] for c in self.catalog), e.CATEGORIES)

    def test_exact_working_seven_pinned(self):
        parsed = e.parse_m3u(self.approved_text)
        self.assertEqual(parsed, self.approved)
        e.validate_approved(parsed, self.pinned, self.evidence)

    def test_reject_drifted_certified_url(self):
        changed = copy.deepcopy(self.approved)
        changed[3]["url"] = "https://somewhere.example/a.m3u8"
        with self.assertRaisesRegex(ValueError, "differs"):
            e.validate_approved(changed, self.pinned, self.evidence)

    def test_reject_changed_first_three(self):
        changed = copy.deepcopy(self.approved)
        changed[0]["name"] = "Changed original"
        with self.assertRaisesRegex(ValueError, "production prefix"):
            e.validate_approved(changed, self.pinned, self.evidence)

    def test_reject_banned_or_duplicate_candidate_ids(self):
        obj = json.loads((ROOT / "expansion-candidates.json").read_text())
        obj["candidates"][0]["tvg_id"] = "ABCNews.us@Free"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"bad.json"
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "duplicates"):
                e.load_catalog(path, {"ABCNews.us@Free"})

    def test_candidate_hostname_allowlist(self):
        obj = json.loads((ROOT / "expansion-candidates.json").read_text())
        obj["candidates"][0]["url"] = "https://evil.example/playlist.m3u8"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"bad.json"
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "Unapproved"):
                e.load_catalog(path, set())

    def test_catalog_disallows_http(self):
        obj = json.loads((ROOT / "expansion-candidates.json").read_text())
        obj["candidates"][0]["url"] = "http://rally-tv-live.akamaized.net/hls/live/test.m3u8"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/"bad.json"
            path.write_text(json.dumps(obj))
            with self.assertRaisesRegex(ValueError, "Unapproved"):
                e.load_catalog(path, set())

    def test_malformed_approved_m3u_rejected(self):
        with self.assertRaisesRegex(ValueError, "Malformed"):
            e.parse_m3u("#EXTM3U\n#EXTINF:-1,broken")

    def test_manifest_pass_only_enters_preview_not_approved(self):
        probes = [e.GOOD_RESPONSE if i in (0,3,7) else "HTTP 403" for i in range(16)]
        out = e.build_expansion(self.approved_text, self.base_guide, self.pinned,
                                self.evidence, self.catalog, probes, None, NOW, "guide unavailable")
        status = json.loads(out["expansion-status.json"])
        playlist = out["expansion.m3u"].decode()
        self.assertEqual(status["expansion_total_channels"], 10)
        self.assertEqual(playlist.count("#EXTINF"), 10)
        self.assertEqual(status["new_candidates_manifest_passed"], 3)
        self.assertNotIn("ABC News Live", playlist)
        self.assertNotIn("CBS News 24/7", playlist)
        self.assertEqual(status["manifest_probe_results"][0]["kodi_playback"], "UNTESTED")

    def test_preview_still_seven_on_zero_passes(self):
        out = e.build_expansion(self.approved_text, self.base_guide, self.pinned,
                                self.evidence, self.catalog, ["HTTP 404"]*16, None, NOW)
        self.assertEqual(out["expansion.m3u"].decode(), self.approved_text)
        self.assertEqual(out["expansion-guide.xml"], e.merge_xmltv(self.base_guide, None, [], NOW, self.approved)[0])

    def test_actual_guide_match_only_for_new_candidate(self):
        new = [self.catalog[0]]
        source = xml_for([("Racer Network", "real-racer-source"), ("ABC News Live", "failed-abc")])
        guide, names, added = e.merge_xmltv(self.base_guide, source, new, NOW, self.approved)
        self.assertEqual(added, ["Racer Network"])
        root = ET.fromstring(guide)
        ids = {x.get("id") for x in root.findall("channel")}
        self.assertIn("RacerNetwork.us@SD", ids)
        self.assertNotIn("ABCNews.us@Free", ids)
        self.assertEqual({x.get("channel") for x in root.findall("programme")}, ids)
        self.assertEqual(len(names), 3)

    def test_ambiguous_external_schedule_is_rejected(self):
        root = ET.fromstring(xml_for([("Racer Network", "racer-1")]))
        extra = ET.SubElement(root, "channel", id="racer-2")
        ET.SubElement(extra,"display-name").text = "Racer Network"
        guide, names, added = e.merge_xmltv(self.base_guide, ET.tostring(root), [self.catalog[0]], NOW, self.approved)
        self.assertEqual(added, [])
        self.assertEqual(len(ET.fromstring(guide).findall("channel")), 2)

    def test_expired_programme_does_not_match(self):
        root = ET.fromstring(xml_for([("Racer Network", "racer-1")]))
        p = root.find("programme")
        p.set("start", (NOW - timedelta(days=3)).strftime("%Y%m%d%H%M%S +0000"))
        p.set("stop", (NOW - timedelta(days=2)).strftime("%Y%m%d%H%M%S +0000"))
        guide, names, added = e.merge_xmltv(self.base_guide, ET.tostring(root), [self.catalog[0]], NOW, self.approved)
        self.assertEqual(added, [])

    def test_no_externally_fabricated_programme(self):
        out = e.build_expansion(self.approved_text, self.base_guide, self.pinned,
                                self.evidence, self.catalog, [e.GOOD_RESPONSE]*16, None, NOW)
        guide = ET.fromstring(out["expansion-guide.xml"])
        self.assertEqual(len(guide.findall("channel")), 2)
        self.assertEqual(len(guide.findall("programme")), 2)
        self.assertEqual(json.loads(out["expansion-status.json"])["expansion_total_channels"],23)

    def test_no_new_candidates_can_change_prior_feed_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d)
            pinned = {"channels.m3u": b"unchanged three", "guide.xml": b"unchanged guide",
                      "approved.m3u": self.approved_text.encode(), "approved-guide.xml": self.base_guide}
            for name,data in pinned.items(): (site/name).write_bytes(data)
            out = e.build_expansion(self.approved_text, self.base_guide, self.pinned,
                                    self.evidence, self.catalog, ["fail"]*16, None, NOW)
            for name,data in out.items(): (site/name).write_bytes(data)
            for name,data in pinned.items(): self.assertEqual((site/name).read_bytes(),data)
            self.assertEqual({p.name for p in site.iterdir()}-set(pinned), set(out))


if __name__ == "__main__":
    unittest.main()
