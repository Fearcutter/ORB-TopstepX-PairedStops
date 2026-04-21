"""Entry point: python -m orb_topstepx  (or: orb-topstepx)"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from .client import TopstepXClient
from .settings import load_credentials
from .ui import PairedStopsWindow, apply_dark_palette


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv)
    apply_dark_palette(app)

    username, api_key = load_credentials()
    if not username or not api_key:
        QMessageBox.critical(
            None,
            "TopstepX credentials missing",
            "Set TOPSTEPX_USERNAME and TOPSTEPX_API_KEY in your environment "
            "or in a .env file in the current directory. See .env.example.",
        )
        return 2

    client = TopstepXClient(username=username, api_key=api_key)
    try:
        window = PairedStopsWindow(client)
    except Exception as ex:
        QMessageBox.critical(None, "Startup failed", str(ex))
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
