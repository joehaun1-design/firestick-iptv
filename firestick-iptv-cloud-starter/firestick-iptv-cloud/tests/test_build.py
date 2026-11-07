import unittest
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import create_guide, create_m3u, read_channels


class IPTVBuildTests(unittest.TestCase):
    def setUp(self):
        self.channels = read_channels(Path(__file__).resolve().parents[1] / "channels.json")
        self.now = datetime(2026, 11, 7, 15, tzinfo=timezone.utc)

    def test_m3u_has_three_known_channels(self):
        m3u = create_m3u(self.channels)
        self.assertEqual(m3u.count("#EXTINF"), 3)
        self.assertIn('tvg-id="NBCSportsNOW.us@SD"', m3u)
        self.assertNotIn("Big Buck Bunny", m3u)

    def test_current_xmltv_is_relabelled(self):
        start = (self.now - timedelta(minutes=15)).strftime("%Y%m%d%H%M%S +0000")
        stop = (self.now + timedelta(minutes=90)).strftime("%Y%m%d%H%M%S +0000")
        source = (f'<tv><channel id="6549306c83595c000815a696">'
                  f'<display-name>NBC Sports NOW</display-name></channel>'
                  f'<programme channel="6549306c83595c000815a696" '
                  f'start="{start}" stop="{stop}"><title>Test Show</title></programme></tv>')
        xml, stats = create_guide(source.encode(), self.channels, self.now)
        parsed = ET.fromstring(xml)
        self.assertEqual(parsed.find("channel").get("id"), "NBCSportsNOW.us@SD")
        self.assertEqual(parsed.find("programme").get("channel"), "NBCSportsNOW.us@SD")
        self.assertEqual(stats["programme_count"], 1)

    def test_stale_guide_rejected(self):
        start = (self.now - timedelta(days=5)).strftime("%Y%m%d%H%M%S +0000")
        stop = (self.now - timedelta(days=4)).strftime("%Y%m%d%H%M%S +0000")
        source = (f'<tv><channel id="6549306c83595c000815a696">'
                  f'<display-name>NBC Sports NOW</display-name></channel>'
                  f'<programme channel="6549306c83595c000815a696" '
                  f'start="{start}" stop="{stop}"><title>Old Show</title></programme></tv>')
        with self.assertRaises(ValueError):
            create_guide(source.encode(), self.channels, self.now)


if __name__ == "__main__":
    unittest.main()
