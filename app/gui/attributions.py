# app/gui/attributions.py — third-party open-source components bundled and
# invoked by the container image. Rendered on the About page and mirrored in
# README's "Third-party software" section (keep the two in sync).
from __future__ import annotations

THIRD_PARTY: list[dict[str, str]] = [
    {"name": "restic", "license": "BSD-2-Clause", "url": "https://restic.net",
     "role": "versioned & encrypted snapshot engine"},
    {"name": "rclone", "license": "MIT", "url": "https://rclone.org",
     "role": "archive / sync engine"},
    {"name": "supercronic", "license": "MIT", "url": "https://github.com/aptible/supercronic",
     "role": "in-container cron scheduler"},
    {"name": "AWS CLI", "license": "Apache-2.0", "url": "https://github.com/aws/aws-cli",
     "role": "S3 usage + Cost Explorer calls"},
    {"name": "OpenTofu", "license": "MPL-2.0", "url": "https://opentofu.org",
     "role": "provisioning (bucket + least-privilege IAM)"},
    {"name": "Flask", "license": "BSD-3-Clause", "url": "https://flask.palletsprojects.com",
     "role": "web GUI framework"},
    {"name": "Fraunces", "license": "OFL-1.1", "url": "https://github.com/undercasetype/Fraunces",
     "role": "GUI typeface (titles and figures)"},
    {"name": "Source Sans 3", "license": "OFL-1.1", "url": "https://github.com/adobe-fonts/source-sans",
     "role": "GUI typeface (text)"},
    {"name": "IBM Plex Mono", "license": "OFL-1.1", "url": "https://github.com/IBM/plex",
     "role": "GUI typeface (paths, ids, commands)"},
    {"name": "Waitress", "license": "ZPL-2.1", "url": "https://github.com/Pylons/waitress",
     "role": "production WSGI server"},
]
