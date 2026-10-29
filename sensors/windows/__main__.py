"""python -m sensors.windows [--check] [--verbose]"""
import sys

from .sensor import main

sys.exit(main())
