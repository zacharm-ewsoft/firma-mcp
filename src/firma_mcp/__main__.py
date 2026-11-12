"""``python -m firma_mcp``: serve MCP over Streamable HTTP at ``/mcp``.

Meant to sit behind a TLS-terminating reverse proxy (see docker-compose.yml and deploy/).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from firma_mcp.errors import ConfigError
from firma_mcp.server import Settings, create_server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    env_file = Path(os.environ.get("FIRMA_ENV_FILE", ".env"))
    if env_file.exists():
        load_dotenv(env_file)
    try:
        settings = Settings.from_env(os.environ)
    except ConfigError as exc:
        # Fail closed: without valid authentication and at least one module there is no server.
        print(f"firma-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    core = settings.core
    server = create_server(settings, background=True)
    logging.getLogger("firma_mcp").info(
        "starting: auth=%s modules=%s url=%s/mcp",
        core.auth_mode,
        ",".join(settings.module_names),
        core.base_url,
    )
    server.run(
        transport="http",
        host=core.host,
        port=core.port,
        path="/mcp",
        # Reject requests whose Host header is not ours (DNS rebinding protection).
        allowed_hosts=[core.public_host, f"{core.host}:{core.port}", f"localhost:{core.port}"],
        show_banner=False,
    )


if __name__ == "__main__":
    main()
