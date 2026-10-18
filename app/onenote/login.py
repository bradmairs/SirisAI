"""One-time Microsoft sign-in for the OneNote import, from the server:

    docker exec -it sirisai-sirisai-1 python -m app.onenote.login

It prints a code to enter at microsoft.com/devicelogin (any device), then
saves a refresh token in SIRISAI_ONENOTE_TOKEN_PATH. Run it again if the
nightly import reports that the sign-in has expired.
"""

from app.core.config import Settings
from app.onenote.graph import TokenStore, device_login


def main() -> None:
    settings = Settings()
    if not settings.onenote_client_id:
        raise SystemExit("Set SIRISAI_ONENOTE_CLIENT_ID first (see README milestone 136).")
    device_login(settings.onenote_client_id, TokenStore(settings.onenote_token_path))
    print(f"Signed in. Siris will import OneNote nightly; the token is in {settings.onenote_token_path}.")


if __name__ == "__main__":
    main()
