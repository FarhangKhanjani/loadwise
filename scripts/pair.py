"""Pair your phone with the LoadWise app: prints a QR code that opens the app already
signed in.

  python -m scripts.pair                                   # finds your Tailscale address
  python -m scripts.pair --url https://my-pc.tail1234.ts.net

The QR code holds the app address plus your API_KEY after a '#': https://.../app/#key=...
Browsers never send the part after '#' to the server, so the key doesn't end up in any
request or log; the app stores it on the phone and removes it from the address bar.

The QR code IS your key: scan it, then close this window. Don't screenshot or share it.
If it ever leaks, put a new API_KEY in .env and restart LoadWise (old pairings stop working).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from src.config import get_settings

MIN_API_KEY_LENGTH = 16
WINDOWS_TAILSCALE = Path(r"C:\Program Files\Tailscale\tailscale.exe")


class PairError(Exception):
    pass


def app_url(base_url: str) -> str:
    """https://my-pc.ts.net -> https://my-pc.ts.net/app/ (whatever the user typed)."""
    base = base_url.strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        base = "https://" + base
    if base.endswith("/app"):
        base = base[: -len("/app")]
    return base + "/app/"


def pairing_link(base_url: str, api_key: str) -> str:
    return f"{app_url(base_url)}#key={quote(api_key, safe='')}"


def tailscale_host(status: dict) -> str:
    """The PC's MagicDNS name from `tailscale status --json`, e.g. my-pc.tail1234.ts.net."""
    name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name:
        raise PairError("Tailscale has no MagicDNS name for this PC. Is MagicDNS on in the admin console?")
    return name


def find_tailscale() -> str:
    found = shutil.which("tailscale")
    if found:
        return found
    if WINDOWS_TAILSCALE.exists():
        return str(WINDOWS_TAILSCALE)
    raise PairError("Tailscale is not installed (https://tailscale.com/download), or pass --url.")


def detect_url() -> str:
    try:
        out = subprocess.run([find_tailscale(), "status", "--json"], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise PairError(f"`tailscale status` failed. Is Tailscale running and signed in? {e.stderr.strip()}")
    return "https://" + tailscale_host(json.loads(out))


def check_key(api_key: str | None) -> str:
    if not api_key or len(api_key) < MIN_API_KEY_LENGTH:
        raise PairError("Set API_KEY in .env first (at least 16 characters). Generate one with:\n"
                        '  python -c "import secrets; print(secrets.token_urlsafe(32))"')
    return api_key


def main() -> None:  # pragma: no cover - prints to the terminal
    import qrcode

    parser = argparse.ArgumentParser(description="Show a QR code that pairs your phone with LoadWise.")
    parser.add_argument("--url", help="Address of LoadWise as the phone sees it (default: from Tailscale)")
    parser.add_argument("--light-terminal", action="store_true",
                        help="Your terminal has a light background (default assumes dark)")
    args = parser.parse_args()
    try:
        key = check_key(get_settings().api_key)
        base = args.url or detect_url()
    except PairError as e:
        sys.exit(str(e))

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(pairing_link(base, key))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the QR code is drawn with block characters
    print(f"\nScan with your phone's camera to open {app_url(base)} already paired:\n")
    qr.print_ascii(invert=not args.light_terminal)  # dark squares must come out dark
    print("This QR code contains your API key. Close this window after scanning; don't share a screenshot.")
    print("Then, in the browser menu, choose 'Add to Home screen' (Android) or Share > 'Add to Home Screen' (iPhone).\n")


if __name__ == "__main__":
    main()
