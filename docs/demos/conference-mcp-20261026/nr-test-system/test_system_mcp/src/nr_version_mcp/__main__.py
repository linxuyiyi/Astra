import logging

from .server import mcp


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
