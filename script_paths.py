"""Resolve the coordinated Agency OS script dependency without hardcoded CI paths."""

import os
import sys


DEFAULT_AGENCY_SCRIPT_DIR = "/home/agency/agency-os/scripts"


def ensure_agency_scripts():
    path = os.environ.get("AGENCY_SCRIPT_DIR", DEFAULT_AGENCY_SCRIPT_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return path
