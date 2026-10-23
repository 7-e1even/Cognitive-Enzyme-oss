"""Entry point for `python -m app` and `enzyme` CLI."""

import logging

import uvicorn
from dotenv import load_dotenv


def main():
    load_dotenv()
    from app.config import API_PORT   # 读 .env 之后再取端口

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    uvicorn.run("app.main:app", host="0.0.0.0", port=API_PORT, log_level="info")


if __name__ == "__main__":
    main()
