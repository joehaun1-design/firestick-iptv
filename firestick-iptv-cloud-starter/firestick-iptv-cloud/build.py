#!/usr/bin/env python3
"""Build a small public M3U + genuine, current XMLTV schedule for GitHub Pages.

Uses only the Python standard library. Fail closed if guide data cannot be
verified; GitHub Pages will retain the last successful deployment.
"""
from __future__ import annotations

import copy
import gzip
import html
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "_site"
CHANNELS_FILE = ROOT / "channels.json"
GUIDE_SOURCE = "https://i.mjh.nz/PlutoTV/us.xml.gz"
NBC_SOURCE_ID = "6549306c83595c000815a696"
MAX_DOWNLOAD = 80 * 1024 * 1024


def read_channels(path: Path) -> list[dict]:
    channels = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(channels, list) or not channels:
        raise ValueError("channels.json must contain a nonempty array")
    ids: set[str] = set()
    for item in channels:
        for key in ("name", "group", "tvg_id", "url"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Missing or invalid channel field: {key}")
            if any(c in item[key] for c in "\r\n\"" if key != "url"):
                raise ValueError(f"Unsafe newline or quote in field {key}")
        if item["tvg_id"] in ids:
            raise ValueError(f"Duplicate tvg_id: {item['tvg_id']}")
        ids.add(item["tvg_id"])
        if urlparse(item["url"]).scheme != "https":
            raise ValueError(f"Only HTTPS stream URLs allowed: {item['name']}")
    return channels


def create_m3u(channels: list[dict]) -> str:
    result = ["#EXTM3U"]
    for ch in channels:
        attrs = (
            f'tvg-id="{ch["tvg_id"]}" '
            f'group-title="{ch["group"]}"'
        )
        result.append(f'#EXTINF:-1 {attrs},{ch["name"]}')
        result.append(ch["url"])
    return "\n".join(result) + "\n"


def download_guide(url: str = GUIDE_SOURCE) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "FireTV-Guide-Builder/1.0"})
    with urllib.request.urlopen(req, timeout=45) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Guide download exceeds safety limit")
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as reader:
            data = reader.read(MAX_DOWNLOAD * 8 + 1)
        if len(data) > MAX_DOWNLOAD * 8:
            raise ValueError("Decompressed guide exceeds safety limit")
    return data


def xmltv_datetime(value: str) -> datetime:
    # Typical XMLTV representation: YYYYMMDDHHMMSS +0000
    stamp = value.strip()
    if re.fullmatch(r"\d{14} [+-]\d{4}", stamp):
        return datetime.strptime(stamp, "%Y%m%d%H%M%S %z").astimezone(timezone.utc)
    if re.fullmatch(r"\d{14}", stamp):
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    raise ValueError(f"Unexpected XMLTV time: {value!r}")


def create_guide(data: bytes, channels: list[dict], now: datetime) -> tuple[bytes, dict]:
    document = ET.fromstring(data)
    if document.tag != "tv":
        raise ValueError("Remote response is not XMLTV")

    # Only import the verified NBC Sports NOW feed; other channels have no
    # verified XMLTV source yet. Do not fabricate schedule entries.
    target = next((c for c in channels if c.get("epg_source") == "pluto_nbc_sports_now"), None)
    if target is None:
        raise ValueError("No NBC Sports NOW channel configured")

    selected = None
    for candidate in document.findall("channel"):
        names = [(node.text or "").strip().lower() for node in candidate.findall("display-name")]
        if (candidate.get("id") == NBC_SOURCE_ID or
                any("nbc sports now" in name for name in names)):
            selected = candidate
            break
    if selected is None:
        raise ValueError("NBC Sports NOW not found in the source EPG")

    source_id = selected.attrib["id"]
    root = ET.Element("tv", {"generator-info-name": "Fire TV curated guide"})
    channel = copy.deepcopy(selected)
    channel.set("id", target["tvg_id"])
    root.append(channel)

    entries = []
    for source_programme in document.findall("programme"):
        if source_programme.get("channel") != source_id:
            continue
        start = xmltv_datetime(source_programme.get("start", ""))
        stop = xmltv_datetime(source_programme.get("stop", ""))
        if stop <= start:
            continue
        if stop < now - timedelta(hours=2) or start > now + timedelta(hours=72):
            continue
        programme = copy.deepcopy(source_programme)
        programme.set("channel", target["tvg_id"])
        entries.append((start, stop, programme))

    # Avoid replacing the last good published guide with stale/empty data.
    if not entries or max(stop for _, stop, _ in entries) < now + timedelta(hours=1):
        raise ValueError("NBC Sports NOW EPG lacks current/upcoming programmes")

    for _, _, programme in sorted(entries, key=lambda e: e[0]):
        root.append(programme)
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml, {
        "programme_count": len(entries),
        "first_programme_utc": min(start for start, _, _ in entries).isoformat(),
        "last_programme_end_utc": max(stop for _, stop, _ in entries).isoformat(),
    }


def render_index(channels: list[dict], now: datetime, stats: dict) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(ch['name'])}</td><td>{html.escape(ch['group'])}</td></tr>"
        for ch in channels
    )
    timestamp = html.escape(now.strftime("%Y-%m-%d %H:%M UTC"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fire TV — Curated Channels</title><style>
body{{font:16px/1.55 system-ui,sans-serif;margin:4em auto;max-width:660px;padding:0 1em;background:#0f172a;color:#f1f5f9}}
a{{color:#93c5fd}}table{{border-collapse:collapse;width:100%}}td,th{{padding:.65em;border-bottom:1px solid #475569;text-align:left}}
small{{color:#cbd5e1}}</style></head><body>
<h1>Fire TV — Curated Channels</h1>
<p>Free, independently hosted live stream references; availability varies.</p>
<p><a href="channels.m3u">Kodi M3U playlist</a> · <a href="guide.xml">XMLTV programme guide</a> · <a href="status.json">Build status</a></p>
<table><thead><tr><th>Channel</th><th>Group</th></tr></thead><tbody>{rows}</tbody></table>
<p><small>Updated {timestamp}. XMLTV schedule currently covers NBC Sports NOW only ({stats['programme_count']} programme entries).</small></p>
<p><small>Public links only. No paid services or credentials are included.</small></p>
</body></html>"""




# Optional V2 preview catalog. These are publicly listed channel streams, NOT
# guaranteed to work or a claim that third parties grant redistribution rights.
# They are never inserted in channels.m3u without a separate promotion decision.
PREVIEW_CANDIDATES = [
    {"name":"beIN SPORTS XTRA", "tvg_id":"beINSPORTSXTRA.us@SD", "group":"Sports", "url":"https://bein-xtra-bein.amagi.tv/playlist.m3u8", "host":"bein-xtra-bein.amagi.tv"},
    {"name":"Racing America", "tvg_id":"RacingAmerica.us@SD", "group":"Sports", "url":"https://lnc-racing-america.tubi.video/playlist.m3u8", "host":"lnc-racing-america.tubi.video"},
    {"name":"Red Bull TV", "tvg_id":"RedBullTV.at@HD", "group":"Sports", "url":"https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8", "host":"rbmn-live.akamaized.net"},
    {"name":"ABC News Live", "tvg_id":"ABCNews.us@Free", "group":"News", "url":"https://abcnews-streams.akamaized.net/hls/live/2023560/abcnewshudson1/master.m3u8", "host":"abcnews-streams.akamaized.net"},
    {"name":"CBS News 24/7", "tvg_id":"CBSNews.us@Free", "group":"News", "url":"https://cbsnews.akamaized.net/hls/live/2020607/cbsnlineup_8/master.m3u8", "host":"cbsnews.akamaized.net"},
    {"name":"NBC News NOW", "tvg_id":"NBCNewsNOW.us@SD", "group":"News", "url":"https://d1bl6tskrpq9ze.cloudfront.net/hls/master.m3u8?ads.xumo_channelId=99984003", "host":"d1bl6tskrpq9ze.cloudfront.net"},
    {"name":"Bloomberg Television", "tvg_id":"BloombergTelevision.us@Free", "group":"News", "url":"https://bloomberg.com/media-manifest/streams/us.m3u8", "host":"bloomberg.com"},
    {"name":"World Channel", "tvg_id":"WorldChannel.us@SD", "group":"Documentaries", "url":"https://world.lls.pbs.org/index.m3u8", "host":"world.lls.pbs.org"},
    {"name":"Reuters TV", "tvg_id":"ReutersTV.us@Free", "group":"News", "url":"https://reuters-reutersnow-1-eu.rakuten.wurl.tv/playlist.m3u8", "host":"reuters-reutersnow-1-eu.rakuten.wurl.tv"},
]


def probe_hls(candidate: dict) -> tuple[dict, str]:
    """Basic publisher/CDN manifest probe; not a guarantee of Kodi playback."""
    if urlsplit(candidate["url"]).scheme != "https" or urlsplit(candidate["url"]).hostname != candidate["host"]:
        return candidate, "rejected: unexpected host or scheme"
    try:
        req = urllib.request.Request(candidate["url"], headers={"User-Agent":"Mozilla/5.0 FireTV-IPTV-Preview/2.0"})
        with urllib.request.urlopen(req, timeout=7) as response:
            if response.status != 200:
                return candidate, f"HTTP {response.status}"
            head = response.read(512)
            if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U"):
                return candidate, "HLS manifest HTTP 200"
            return candidate, "response did not start with #EXTM3U"
    except Exception as exc:
        return candidate, f"unavailable: {type(exc).__name__}"


def select_preview_candidates(probe_results: list[tuple[dict, str]], pinned: list[dict]) -> list[dict]:
    seen = {p["tvg_id"] for p in pinned}
    selected = []
    for candidate, result in probe_results:
        if result != "HLS manifest HTTP 200" or candidate["tvg_id"] in seen:
            continue
        seen.add(candidate["tvg_id"])
        selected.append({key: candidate[key] for key in ("name","tvg_id","group","url")})
    return selected


def _key_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def create_preview_guide(source_xml: bytes, base_guide: bytes, candidates: list[dict], now: datetime) -> tuple[bytes, list[str]]:
    """Use only uniquely matching real source display names, never fake titles."""
    root = ET.fromstring(source_xml)
    base = ET.fromstring(base_guide)
    matches = {}
    for channel in root.findall("channel"):
        for display in channel.findall("display-name"):
            key = _key_name(display.text or "")
            if key:
                matches.setdefault(key, []).append(channel)
    programming = {}
    for programme in root.findall("programme"):
        programming.setdefault(programme.get("channel"), []).append(programme)
    populated = ["NBC Sports NOW"]
    for candidate in candidates:
        key = _key_name(candidate["name"])
        possibilities = {node.get("id"):node for node in matches.get(key, [])}
        if len(possibilities) != 1:
            continue
        source_id, source_channel = next(iter(possibilities.items()))
        eligible = []
        for raw in programming.get(source_id, []):
            try:
                start = xmltv_datetime(raw.get("start", ""))
                stop = xmltv_datetime(raw.get("stop", ""))
            except ValueError:
                continue
            if start < now + timedelta(hours=72) and stop > now - timedelta(hours=2) and stop > start:
                cloned = copy.deepcopy(raw)
                cloned.set("channel", candidate["tvg_id"])
                eligible.append((start, stop, cloned))
        if not eligible or max(stop for _, stop, _ in eligible) < now + timedelta(hours=1):
            continue
        ch = copy.deepcopy(source_channel)
        ch.set("id", candidate["tvg_id"])
        base.append(ch)
        for _, _, programme in sorted(eligible, key=lambda x:x[0]):
            base.append(programme)
        populated.append(candidate["name"])
    ET.indent(base, space="  ")
    return ET.tostring(base, encoding="utf-8", xml_declaration=True), populated


def write_v2_preview(source_xml: bytes, base_guide: bytes, pinned: list[dict], now: datetime) -> dict:
    # Parallel probes keep the GitHub scheduled build short; failures do not
    # change the already proven three-channel list or its XMLTV guide.
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(probe_hls, PREVIEW_CANDIDATES))
    candidates = select_preview_candidates(results, pinned)
    preview_guide, guide_names = create_preview_guide(source_xml, base_guide, candidates, now)
    (DEST / "preview.m3u").write_text(create_m3u(pinned + candidates), encoding="utf-8")
    (DEST / "preview-guide.xml").write_bytes(preview_guide)
    report = {
        "generated_utc": now.isoformat(),
        "known_working_channels": len(pinned),
        "candidate_channels_passed_basic_http_hls_probe": len(candidates),
        "preview_total_channels": len(pinned) + len(candidates),
        "preview_epg_channel_names": guide_names,
        "candidates": [{"name": item["name"], "result": result} for item, result in results],
        "caution": "HTTP probe results do not prove live video works on your Fire Stick. Only after manual Kodi testing should candidates be promoted."
    }
    (DEST / "preview-status.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    now = datetime.now(timezone.utc)
    channels = read_channels(CHANNELS_FILE)
    data = download_guide()
    xml, stats = create_guide(data, channels, now)
    DEST.mkdir(exist_ok=True)
    (DEST / "channels.m3u").write_text(create_m3u(channels), encoding="utf-8")
    (DEST / "guide.xml").write_bytes(xml)
    (DEST / "index.html").write_text(render_index(channels, now, stats), encoding="utf-8")
    status = {
        "generated_utc": now.isoformat(),
        "channel_count": len(channels),
        "epg_channels": ["NBC Sports NOW"],
        **stats,
        "note": "Only the NBC Sports NOW channel has verified programme schedule data."
    }
    (DEST / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    preview = write_v2_preview(data, xml, channels, now)
    print(f"Preview candidates: {preview['candidate_channels_passed_basic_http_hls_probe']} candidates passed basic HTTP manifest probes")
    print(f"Build OK: {len(channels)} channels, {stats['programme_count']} current EPG entries")
    print(f"Guide coverage ends: {stats['last_programme_end_utc']}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ET.ParseError, TimeoutError) as exc:
        print(f"Build failed safely; no new site deployed: {exc}", file=sys.stderr)
        sys.exit(1)
