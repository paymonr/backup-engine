# app/gui/server.py — waitress entrypoint: `python3 -m app.gui.server`
from __future__ import annotations
import os
from waitress import serve
from app.gui import create_app

def main() -> None:
    serve(create_app(), host="0.0.0.0", port=int(os.environ.get("GUI_PORT", "8099")))

if __name__ == "__main__":
    main()
