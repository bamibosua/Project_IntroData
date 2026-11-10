"""Allow `python -m src.downloader`."""

from .cli import main
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
raise SystemExit(main())
