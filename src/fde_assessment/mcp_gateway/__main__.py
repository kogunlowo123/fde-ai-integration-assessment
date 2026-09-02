"""``python -m fde_assessment.mcp_gateway``, run the gateway with uvicorn."""

from __future__ import annotations

import uvicorn

from fde_assessment.common.config import get_settings
from fde_assessment.common.logging import configure_logging
from fde_assessment.mcp_gateway.app import create_app


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.mcp_gateway_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
