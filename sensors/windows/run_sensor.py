#!/usr/bin/env python3
"""Run the Windows sensor from a checkout: python run_sensor.py [--check] [--verbose]."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sensors.windows.sensor import main

if __name__ == '__main__':
    sys.exit(main())
