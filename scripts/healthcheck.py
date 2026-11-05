"""Проверка для Docker HEALTHCHECK: веб-сервер бота отвечает на /health."""
from __future__ import annotations

import os
import sys
import urllib.request

port = os.environ.get("WEB_PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
