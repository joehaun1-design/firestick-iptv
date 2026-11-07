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
