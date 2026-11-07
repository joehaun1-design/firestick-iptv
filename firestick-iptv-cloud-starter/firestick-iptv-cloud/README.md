# Fire TV curated IPTV — no Windows server required

This project publishes the **three streams already tested on the user's Fire TV Stick** (France 24 English, DW English, NBC Sports NOW) and a **small, genuine NBC Sports NOW XMLTV schedule**. Other channels have no EPG yet. It contains no subscriptions, piracy sources, passwords, or private playlist tokens.

## What it does

- Generates `channels.m3u` from `channels.json` (News + Sports).
- Fetches the current public Pluto TV XMLTV guide from `https://i.mjh.nz/PlutoTV/us.xml.gz`.
- Extracts only the NBC Sports NOW schedule, and rewrites its `channel` ID to match the M3U.
- **Refuses to deploy a stale/empty guide.** The previously published GitHub Pages deployment stays available on a failed update, although its guide eventually becomes stale.
- Deploys using GitHub Pages Actions at 08:17 and 20:17 UTC, plus manual runs and pushes to `main`.

## Setup via GitHub

1. Make a **public GitHub repository** named `firestick-iptv` (GitHub Pages is free for public repositories). Do **not** put private subscription credentials or personal information in the repo.
2. Upload the **contents** of this project, preserving the `.github/workflows/deploy.yml` path. GitHub Desktop or Git is usually easiest for uploading folders. For GitHub Desktop, use **File → Add local repository → Create a repository here**, then **Publish repository** with **Keep this code private UNCHECKED**.
3. On GitHub, go to repository **Settings → Pages → Build and deployment → Source → GitHub Actions** (not “Deploy from a branch”).
4. Go to **Actions → Publish curated IPTV and fresh EPG → Run workflow** if it did not run automatically on the initial push. Wait until it reports success. If it fails, inspect that run's logs; do not point Kodi at an unpublished URL.
5. Open your GitHub Pages homepage shown under **Settings → Pages** to verify it displays all three channels. For a repository named `firestick-iptv`, URLs normally resemble:
   - `https://YOUR_USERNAME.github.io/firestick-iptv/channels.m3u`
   - `https://YOUR_USERNAME.github.io/firestick-iptv/guide.xml`
6. In Kodi 21.3, **edit the existing Windows IPTV Test configuration** (the only enabled one) and replace its M3U and XMLTV URLs with the two Pages URLs above. Set auto-refresh to **Once per day**, if available; keep the two earlier failing configurations disabled. Exit and relaunch Kodi once to confirm channels and guide. Rename the configuration to “Hosted IPTV” if you wish.
7. After you have verified playback and the guide in Kodi using GitHub Pages, you can stop the old Windows Python server with `Ctrl+C`. Your Windows PC is then optional.

> GitHub Pages deployment is public, and the source URLs must be publicly shareable. A scheduled GitHub Actions workflow can be delayed or disabled after 60 days of repository inactivity. If that happens, re-enable it and run the workflow manually. A working public channel stream can still disappear or change independently of this repository.

## Verify locally (optional)

```powershell
py -3 -m unittest discover -s tests -v
py -3 build.py
```

A successful build writes `_site/channels.m3u`, `_site/guide.xml`, `_site/index.html`, and `_site/status.json`. Python 3 is the only dependency. If the guide source is unreachable or stale, the build fails instead of publishing an empty guide.

## Updating the lineup

Edit `channels.json`, preserving unique `tvg_id` values. Only add freely and legitimately published stream URLs that you have permission to use. Pushing a change to `main` triggers a fresh build/deployment. A new channel won't have schedule data until a verified source is integrated into `build.py`.

## Troubleshooting

- **Pages returns 404**: Check Settings → Pages has **GitHub Actions** selected, and that the latest deployment succeeded.
- **NBC guide blank**: Check the live build's `status.json`. The source may be stale, inaccessible, or identifiers may have changed.
- **Other channels have no guide**: Expected. Only NBC Sports NOW is wired to a real EPG source initially.
- **Kodi channels disappear**: Confirm the hosted M3U and guide URLs load in a browser; check the two older configurations remain disabled.
- **Windows server still needed?** No, after Kodi is switched to GitHub Pages. The channels stream directly from their broadcasters and not through GitHub Pages or Windows.

## Sources

- Kodi IPTV Simple Client docs: https://github.com/kodi-pvr/pvr.iptvsimple
- GitHub Pages custom workflows: https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages
- GitHub scheduled-workflow notes: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
