#!/usr/bin/env python3
"""Fire TV IPTV v2.1: pinned production + user-certified approved lineup.

Uses only the Python standard library. Source-locked playback results govern
approvals; network HLS probes are advisory, never promotion authority.
Fail-closed CI leaves the previously published GitHub Pages site in place.
"""
from __future__ import annotations

import copy
import gzip
import html
import io
import json
import re
import sys
import shutil
import tempfile
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
PLAYBACK_FILE = ROOT / "playback-results.json"
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


def read_playback_results(path: Path, candidates: list[dict], pinned: list[dict]) -> dict[str, str]:
    """Read human Kodi playback evidence, pinned to *exact* tested stream URLs.

    Stream URLs changing after tests must stop publication rather than silently
    transfer a previous successful playback certificate to a new endpoint.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("results"), list):
        raise ValueError("Invalid playback-results.json schema")
    lookup = {ch["tvg_id"]: ch for ch in candidates}
    if len(lookup) != len(candidates):
        raise ValueError("Duplicate candidate tvg_id")
    pinned_ids = {ch["tvg_id"] for ch in pinned}
    outcomes: dict[str, str] = {}
    for record in data["results"]:
        if not isinstance(record, dict):
            raise ValueError("Playback record must be an object")
        identifier = record.get("tvg_id")
        result = record.get("result")
        if identifier in outcomes or identifier in pinned_ids:
            raise ValueError(f"Duplicate or pinned playback ID: {identifier}")
        if result not in ("works", "fails"):
            raise ValueError(f"Invalid playback result: {result}")
        candidate = lookup.get(identifier)
        if not candidate:
            raise ValueError(f"Unknown candidate in playback evidence: {identifier}")
        if (record.get("name") != candidate["name"] or
                record.get("url") != candidate["url"]):
            raise ValueError(f"Changed URL/name since Kodi test: {identifier}")
        outcomes[identifier] = result
    if not outcomes:
        raise ValueError("Playback evidence is empty")
    return outcomes


def select_approved_channels(pinned: list[dict], candidates: list[dict], outcomes: dict[str, str]) -> list[dict]:
    """Only Kodi-confirmed 'works' records can be added to the approved feed."""
    additions = [
        {key: ch[key] for key in ("name", "tvg_id", "group", "url")}
        for ch in candidates if outcomes.get(ch["tvg_id"]) == "works"
    ]
    approved = pinned + additions
    if len({ch["tvg_id"] for ch in approved}) != len(approved):
        raise ValueError("Duplicate approved tvg_id")
    return approved


def select_preview_candidates(
    probe_results: list[tuple[dict, str]], approved: list[dict], outcomes: dict[str, str]
) -> list[dict]:
    """Only new, unreviewed manifest-pass streams go into discovery preview."""
    seen = {p["tvg_id"] for p in approved}
    discovered = []
    for candidate, result in probe_results:
        identifier = candidate["tvg_id"]
        if (identifier in seen or identifier in outcomes or
                result != "HLS manifest HTTP 200"):
            continue
        seen.add(identifier)
        discovered.append({key: candidate[key] for key in ("name", "tvg_id", "group", "url")})
    return discovered


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


def make_v21_outputs(source_xml: bytes, pinned_guide: bytes, pinned: list[dict], now: datetime) -> dict:
    """Return complete publishable outputs without modifying the filesystem."""
    outcomes = read_playback_results(PLAYBACK_FILE, PREVIEW_CANDIDATES, pinned)
    approved = select_approved_channels(pinned, PREVIEW_CANDIDATES, outcomes)
    if len(approved) <= len(pinned):
        raise ValueError("No approved Kodi-tested additions")

    # Match guide schedules against actual source XML; no invented programmes.
    approved_guide, approved_guide_names = create_preview_guide(
        source_xml, pinned_guide, approved[len(pinned):], now
    )

    # Probing is advisory only: it cannot add/remove Kodi-approved channels.
    with ThreadPoolExecutor(max_workers=5) as pool:
        probes = list(pool.map(probe_hls, PREVIEW_CANDIDATES))
    discoveries = select_preview_candidates(probes, approved, outcomes)
    preview = approved + discoveries
    preview_guide, preview_guide_names = create_preview_guide(
        source_xml, pinned_guide, preview[len(pinned):], now
    )

    rejected = [ch["name"] for ch in PREVIEW_CANDIDATES if outcomes.get(ch["tvg_id"]) == "fails"]
    certified = [ch["name"] for ch in approved[len(pinned):]]
    status = {
        "generated_utc": now.isoformat(),
        "pinned_production_channels": len(pinned),
        "approved_total_channels": len(approved),
        "new_kodi_verified_channels": certified,
        "kodi_playback_failed_channels": rejected,
        "approved_guide_channel_names": approved_guide_names,
        "approval_rule": "Exact recorded name, tvg_id and stream URL with Kodi playback result 'works'; HTTP probes never approve or revoke playback."
    }
    preview_status = {
        "generated_utc": now.isoformat(),
        "approved_channels": len(approved),
        "unreviewed_discovery_channels": len(discoveries),
        "preview_total_channels": len(preview),
        "preview_epg_channel_names": preview_guide_names,
        "candidate_probe_results": [
            {"name": candidate["name"], "manifest_probe": result,
             "kodi_playback": outcomes.get(candidate["tvg_id"], "untested")}
            for candidate, result in probes
        ],
        "caution": "An HTTP/HLS manifest response is not proof of Kodi playback. Failed Kodi streams are excluded."
    }
    return {
        "approved.m3u": create_m3u(approved).encode("utf-8"),
        "approved-guide.xml": approved_guide,
        "approved-status.json": (json.dumps(status, indent=2) + "\n").encode("utf-8"),
        "preview.m3u": create_m3u(preview).encode("utf-8"),
        "preview-guide.xml": preview_guide,
        "preview-status.json": (json.dumps(preview_status, indent=2) + "\n").encode("utf-8"),
    }


def render_v21_index(pinned: list[dict], status: dict, now: datetime) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(ch['name'])}</td><td>{html.escape(ch['group'])}</td></tr>"
        for ch in pinned
    )
    timestamp = html.escape(now.strftime("%Y-%m-%d %H:%M UTC"))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fire TV — Cloud IPTV Manager</title><style>
body{{font:16px/1.5 system-ui,sans-serif;margin:3em auto;max-width:680px;padding:0 1em;background:#0f172a;color:#f1f5f9}}
a{{color:#93c5fd}}table{{border-collapse:collapse;width:100%}}td,th{{padding:.65em;border-bottom:1px solid #475569;text-align:left}}
small{{color:#cbd5e1}}</style></head><body>
<h1>Fire TV — Cloud IPTV Manager v2.1</h1>
<p><strong>Production:</strong> {len(pinned)} original Kodi-tested channels. <a href="channels.m3u">Production M3U</a> · <a href="guide.xml">Production XMLTV</a></p>
<p><strong>Approved:</strong> {status['approved_total_channels']} Kodi-tested channels. <a href="approved.m3u">Approved M3U</a> · <a href="approved-guide.xml">Approved XMLTV</a> · <a href="approved-status.json">Approval report</a></p>
<p><strong>Discovery preview:</strong> <a href="preview.m3u">Preview M3U</a> · <a href="preview-guide.xml">Preview XMLTV</a> · <a href="preview-status.json">Probe report</a></p>
<table><thead><tr><th>Original production channel</th><th>Group</th></tr></thead><tbody>{rows}</tbody></table>
<p><small>Updated {timestamp}. Guide listings only where genuinely sourced; no paid/private streams.</small></p>
</body></html>"""


def publish_site(outputs: dict[str, bytes]) -> None:
    """Stage complete site before replacement; CI only deploys a successful build."""
    stage = Path(tempfile.mkdtemp(prefix="site-v21-", dir=ROOT))
    try:
        for name, data in outputs.items():
            if "/" in name or "\\" in name or name.startswith("."):
                raise ValueError("Invalid output filename")
            (stage / name).write_bytes(data)
        if DEST.exists():
            shutil.rmtree(DEST)
        stage.rename(DEST)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    now = datetime.now(timezone.utc)
    pinned = read_channels(CHANNELS_FILE)
    source_xml = download_guide()
    pinned_guide, stats = create_guide(source_xml, pinned, now)
    additions = make_v21_outputs(source_xml, pinned_guide, pinned, now)
    approved_status = json.loads(additions["approved-status.json"])
    production_status = {
        "generated_utc": now.isoformat(),
        "channel_count": len(pinned),
        "epg_channels": ["NBC Sports NOW"],
        **stats,
        "note": "The original production M3U and EPG remain isolated from v2.1 approval/discovery."
    }
    outputs = {
        "channels.m3u": create_m3u(pinned).encode("utf-8"),
        "guide.xml": pinned_guide,
        "index.html": render_v21_index(pinned, approved_status, now).encode("utf-8"),
        "status.json": (json.dumps(production_status, indent=2) + "\n").encode("utf-8"),
        **additions,
    }
    publish_site(outputs)
    print(f"Build OK: {len(pinned)} pinned; {approved_status['approved_total_channels']} approved; "
          f"{stats['programme_count']} production EPG entries")
    print("Approved channels: " + ", ".join(approved_status["new_kodi_verified_channels"]))
    print("Guide coverage ends: " + stats["last_programme_end_utc"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ET.ParseError, TimeoutError) as exc:
        print(f"Build failed safely; no new site deployed: {exc}", file=sys.stderr)
        sys.exit(1)
