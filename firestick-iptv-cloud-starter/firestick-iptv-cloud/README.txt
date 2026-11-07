Fire Stick IPTV Cloud Manager v2 — verified replacement

This ZIP contains the COMPLETE build.py (not a patch or excerpt).

Extract build.py and replace ONLY:
firestick-iptv-cloud-starter/firestick-iptv-cloud/build.py
inside your GitHub repository.

Do not upload the ZIP itself. Do not upload the README or SHA256 files to GitHub.

PowerShell verification (from folder containing build.py):
  (Get-FileHash .\build.py -Algorithm SHA256).Hash

Expected SHA-256:
693B158631BA229F553BD096B4E276EC91040FB05ACAD3CFD92C3562C603BCDC

The top of the script begins with #!/usr/bin/env python3; the ending contains sys.exit(1).

Your original Kodi channels.m3u and guide.xml are not modified by this package. The GitHub workflow builds and publishes them after it passes.
