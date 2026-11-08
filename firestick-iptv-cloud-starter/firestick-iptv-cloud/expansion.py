#!/usr/bin/env python3
"""v2.2: isolated, best-effort expansion preview for the certified v2.1 feed.

Runs after build.py, never edits its existing files. A valid HLS manifest merely
qualifies a stream for the *unapproved* expansion preview, not approved.m3u.
Never treats a failed stream as working or invents XMLTV programme schedules.
"""
from __future__ import annotations

import copy
import gzip
import io
import ipaddress
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "_site"
CATALOG = ROOT / "expansion-candidates.json"
PRODUCTION = ROOT / "channels.json"
EVIDENCE = ROOT / "playback-results.json"
XMLTV_SOURCE = "https://i.mjh.nz/PlutoTV/us.xml.gz"
GOOD_RESPONSE = "HLS manifest HTTP 200"
MAX_GUIDE_BYTES = 80 * 1024 * 1024
MAX_UNZIPPED_BYTES = 640 * 1024 * 1024
CATEGORIES = {"Sports", "Motorsports", "Movies", "Documentaries", "Entertainment"}
# Only explicitly curated HTTPS broadcast/FAST hosts. No arbitrary host
# discovery, local addresses, credentials, or private URLs.
ALLOWED_HOSTS = {
    "amg00378-mavtv-amg00378c3-sportstribal-emea-1325.playouts.now.amagi.tv",
    "amg00378-amg00378c4-freelivesports-emea-5637.playouts.now.amagi.tv",
    "stream.ads.ottera.tv", "dai.google.com",
    "aegis-cloudfront-1.tubi.video", "1593604785.rsc.cdn77.org",
    "d2tv4k5moji5m7.cloudfront.net", "dbrb49pjoymg4.cloudfront.net",
    "mmn1-301f.kxcdn.com", "cdn.whiplash.cc",
    "ef79b15c8c7c46c7a9de9d33001dbd07.mediatailor.us-west-2.amazonaws.com",
    "1d153317c8db4250b3789601274e2402.mediatailor.us-west-2.amazonaws.com",
    "www.funroads-hls1.com", "plex-xumoent-ch485-e7l5l.fast.nbcuni.com",
    "classicarts.akamaized.net",
}
EXTINF = re.compile(r'^#EXTINF:-1 tvg-id="([^"\r\n]+)" group-title="([^"\r\n]+)",([^\r\n]+)$')
TIME = re.compile(r"\d{14} [+-]\d{4}$")


def parse_m3u(raw: str) -> list[dict]:
    lines = [s.strip() for s in raw.splitlines() if s.strip()]
    if not lines or lines[0] != "#EXTM3U" or (len(lines)-1) % 2:
        raise ValueError("Malformed approved M3U")
    found = []
    seen = set()
    for i in range(1, len(lines), 2):
        match = EXTINF.fullmatch(lines[i])
        url = lines[i+1]
        if not match or urlsplit(url).scheme != "https":
            raise ValueError("Malformed approved M3U channel")
        identifier, group, name = match.groups()
        if identifier in seen:
            raise ValueError("Duplicate approved M3U ID")
        seen.add(identifier)
        found.append({"tvg_id": identifier, "group": group, "name": name, "url": url})
    return found


def validate_approved(approved: list[dict], production: list[dict], evidence: dict) -> None:
    """Pin the published 7-channel input to the Fire Stick's exact test evidence."""
    if len(approved) != 7 or len(production) != 3:
        raise ValueError("Approved/production baseline must contain 7/3 channels")
    if approved[:3] != [
        {key: p[key] for key in ("tvg_id", "group", "name", "url")}
        for p in production
    ]:
        raise ValueError("Original 3-channel production prefix changed")
    if evidence.get("schema_version") != 1:
        raise ValueError("Invalid Kodi playback evidence version")
    results = evidence.get("results")
    if not isinstance(results, list) or len(results) != 6:
        raise ValueError("Expected six Kodi playback test records")
    tested = {}
    for item in results:
        if not isinstance(item, dict) or item.get("result") not in {"works", "fails"}:
            raise ValueError("Malformed Kodi playback evidence")
        ident = item.get("tvg_id")
        if not isinstance(ident, str) or ident in tested:
            raise ValueError("Duplicate Kodi playback ID")
        tested[ident] = item
    certified = {i: d for i, d in tested.items() if d["result"] == "works"}
    rejected = {i for i, d in tested.items() if d["result"] == "fails"}
    if len(certified) != 4 or len(rejected) != 2:
        raise ValueError("Unexpected approval/rejection count")
    if {c["tvg_id"] for c in approved[3:]} != set(certified):
        raise ValueError("Approved feeds do not match four Kodi-certified IDs")
    for ch in approved[3:]:
        entry = certified[ch["tvg_id"]]
        if ch["name"] != entry.get("name") or ch["url"] != entry.get("url"):
            raise ValueError("Approved URL/name differs from actual Kodi test")
    if rejected & {c["tvg_id"] for c in approved}:
        raise ValueError("Rejected Kodi stream found in approved M3U")


def load_catalog(path: Path, disallow_ids: set[str]) -> list[dict]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if obj.get("schema_version") != 1 or not isinstance(obj.get("candidates"), list):
        raise ValueError("Invalid expansion catalog format")
    candidates = obj["candidates"]
    if not candidates or len(candidates) > 40:
        raise ValueError("Expected 1 to 40 reviewed expansion entries")
    ids = set(disallow_ids)
    for ch in candidates:
        if not isinstance(ch, dict) or set(ch) != {"name", "tvg_id", "group", "url"}:
            raise ValueError("Malformed expansion entry")
        for k in ("name", "tvg_id", "group", "url"):
            if not isinstance(ch[k], str) or not ch[k] or any(x in ch[k] for x in "\r\n\""):
                raise ValueError("Unsafe expansion channel field")
        if ch["group"] not in CATEGORIES:
            raise ValueError("Unreviewed expansion category")
        if ch["tvg_id"] in ids:
            raise ValueError("Candidate duplicates approved, denied or another candidate")
        ids.add(ch["tvg_id"])
        u = urlsplit(ch["url"])
        if (u.scheme != "https" or u.hostname not in ALLOWED_HOSTS or
                u.username or u.password or u.port not in (None, 443)):
            raise ValueError("Unapproved or non-HTTPS candidate host")
        try:
            ipaddress.ip_address(u.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("IP literal not permitted")
    return candidates


def probe_hls(ch: dict) -> str:
    """HTTP check only. Its result is never a playback certificate."""
    try:
        req = Request(ch["url"], headers={"User-Agent": "Mozilla/5.0 (FireTV Preview Probe)",
                                          "Accept": "application/vnd.apple.mpegurl,*/*"})
        with urlopen(req, timeout=9) as response:
            if response.status != 200:
                return f"HTTP {response.status}"
            final = urlsplit(response.geturl())
            if final.scheme != "https":
                return "redirected away from HTTPS"
            part = response.read(2048)
            if not part.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U"):
                return "not an HLS manifest"
            return GOOD_RESPONSE
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}"


def render_m3u(channels: list[dict]) -> str:
    result = ["#EXTM3U"]
    for ch in channels:
        result.extend([
            f'#EXTINF:-1 tvg-id="{ch["tvg_id"]}" group-title="{ch["group"]}",{ch["name"]}',
            ch["url"],
        ])
    return "\n".join(result) + "\n"


def parse_time(value: str) -> datetime:
    if not TIME.fullmatch(value):
        raise ValueError("Invalid programme time")
    return datetime.strptime(value, "%Y%m%d%H%M%S %z").astimezone(timezone.utc)


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def merge_xmltv(
    base_xml: bytes, source_xml: bytes | None, added: list[dict], now: datetime,
    approved: list[dict]
) -> tuple[bytes, list[str], list[str]]:
    """Copy genuine optional schedule entries only if source names match uniquely."""
    base = ET.fromstring(base_xml)
    if base.tag != "tv":
        raise ValueError("Approved XMLTV is invalid")
    existing = [node.get("id") for node in base.findall("channel")]
    approved_ids = {ch["tvg_id"] for ch in approved}
    if (len(existing) != len(set(existing)) or
            not set(existing).issubset(approved_ids) or
            "NBCSportsNOW.us@SD" not in existing):
        raise ValueError("Approved guide channel IDs are not coherent")
    names = {c["tvg_id"]: c["name"] for c in approved + added}
    new_guide_names = []
    if source_xml is not None:
        source = ET.fromstring(source_xml)
        if source.tag != "tv":
            raise ValueError("Optional remote XMLTV document malformed")
        found: dict[str, dict[str, ET.Element]] = {}
        for ch in source.findall("channel"):
            ident = ch.get("id")
            if not ident:
                continue
            for display in ch.findall("display-name"):
                key = normalize(display.text or "")
                if key:
                    found.setdefault(key, {})[ident] = ch
        by_channel: dict[str, list[ET.Element]] = {}
        for prog in source.findall("programme"):
            by_channel.setdefault(prog.get("channel", ""), []).append(prog)
        for ch in added:
            key = normalize(ch["name"])
            hits = found.get(key, {})
            if len(hits) != 1 or ch["tvg_id"] in existing:
                continue
            source_id, source_ch = next(iter(hits.items()))
            elig = []
            for p in by_channel.get(source_id, []):
                try:
                    start = parse_time(p.get("start", ""))
                    stop = parse_time(p.get("stop", ""))
                except ValueError:
                    continue
                if (stop > start and stop >= now + timedelta(hours=1) and
                        start < now + timedelta(hours=72)):
                    item = copy.deepcopy(p)
                    item.set("channel", ch["tvg_id"])
                    elig.append((start, item))
            if not elig:
                continue
            channel = copy.deepcopy(source_ch)
            channel.set("id", ch["tvg_id"])
            base.append(channel)
            for _, programme in sorted(elig, key=lambda x: x[0]):
                base.append(programme)
            existing.append(ch["tvg_id"])
            new_guide_names.append(ch["name"])
    ET.indent(base, space="  ")
    result = ET.tostring(base, encoding="utf-8", xml_declaration=True)
    all_guided_names = [names[i] for i in existing]
    return result, all_guided_names, new_guide_names


def fetch_optional_xmltv() -> bytes:
    req = Request(XMLTV_SOURCE, headers={"User-Agent": "FireTV-Expansion-Guide/2.2"})
    with urlopen(req, timeout=30) as response:
        raw = response.read(MAX_GUIDE_BYTES + 1)
    if len(raw) > MAX_GUIDE_BYTES:
        raise ValueError("Guide compressed download too large")
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as reader:
            raw = reader.read(MAX_UNZIPPED_BYTES + 1)
    if len(raw) > MAX_UNZIPPED_BYTES:
        raise ValueError("Guide decompressed download too large")
    return raw


def build_expansion(
    approved_m3u: str, approved_xml: bytes, production: list[dict], evidence: dict,
    candidates: list[dict], probes: list[str], source_xml: bytes | None,
    now: datetime, guide_fetch_status: str = "available"
) -> dict[str, bytes]:
    approved = parse_m3u(approved_m3u)
    validate_approved(approved, production, evidence)
    if len(probes) != len(candidates):
        raise ValueError("Probe results do not match catalog")
    passed = [candidate for candidate, result in zip(candidates, probes) if result == GOOD_RESPONSE]
    lineup = approved + passed
    expanded_guide, all_guides, new_guides = merge_xmltv(
        approved_xml, source_xml, passed, now, approved
    )
    output = {
        "generated_utc": now.isoformat(),
        "source_catalog": "https://github.com/iptv-org/iptv (streams/us.m3u and streams/de.m3u)",
        "approved_playback_certified": len(approved),
        "new_candidates_considered": len(candidates),
        "new_candidates_manifest_passed": len(passed),
        "expansion_total_channels": len(lineup),
        "real_guide_channel_names": all_guides,
        "new_guide_channel_names": new_guides,
        "guide_fetch_status": guide_fetch_status,
        "manifest_probe_results": [
            {"name": ch["name"], "category": ch["group"],
             "manifest_result": result, "kodi_playback": "UNTESTED"}
            for ch, result in zip(candidates, probes)
        ],
        "publication_boundary": "Preview only. No new stream is playback certified. Production and approved feeds remain unchanged.",
    }
    return {
        "expansion.m3u": render_m3u(lineup).encode("utf-8"),
        "expansion-guide.xml": expanded_guide,
        "expansion-status.json": (json.dumps(output, indent=2) + "\n").encode("utf-8"),
    }


def main() -> None:
    if not all((DEST / x).is_file() for x in ("approved.m3u", "approved-guide.xml")):
        raise ValueError("v2.1 approved site files missing; run build.py first")
    approved_raw = (DEST / "approved.m3u").read_text(encoding="utf-8")
    approved = parse_m3u(approved_raw)
    production = json.loads(PRODUCTION.read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    validate_approved(approved, production, evidence)
    denied_ids = {c["tvg_id"] for c in evidence["results"] if c["result"] == "fails"}
    candidates = load_catalog(CATALOG, {c["tvg_id"] for c in approved} | denied_ids)
    with ThreadPoolExecutor(max_workers=5) as pool:
        probes = list(pool.map(probe_hls, candidates))
    try:
        source_xml = fetch_optional_xmltv()
        guide_status = "downloaded from source"
    except (ValueError, OSError, TimeoutError) as exc:
        source_xml = None
        guide_status = f"optional guide not available: {type(exc).__name__}"
    try:
        output = build_expansion(approved_raw, (DEST / "approved-guide.xml").read_bytes(),
                                 production, evidence, candidates, probes, source_xml,
                                 datetime.now(timezone.utc), guide_status)
    except ET.ParseError:
        # Optional external guide may be garbled. Preserve approved XMLTV only.
        output = build_expansion(approved_raw, (DEST / "approved-guide.xml").read_bytes(),
                                 production, evidence, candidates, probes, None,
                                 datetime.now(timezone.utc), "optional guide invalid XML")
    # Stage all three distinct expansion files, then replace only those names.
    # If an earlier action fails, original seven-channel site stays untouched.
    for name, contents in output.items():
        temporary = DEST / (name + ".tmp")
        temporary.write_bytes(contents)
        temporary.replace(DEST / name)
    info = json.loads(output["expansion-status.json"])
    print(f"Expansion v2.2: {info['approved_playback_certified']} proven + "
          f"{info['new_candidates_manifest_passed']} manifest-pass candidates = "
          f"{info['expansion_total_channels']} preview entries")
    print("Additional XMLTV matches: " + ", ".join(info["new_guide_channel_names"]))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"Expansion build failed; no Pages deployment: {exc}", file=sys.stderr)
        sys.exit(1)
