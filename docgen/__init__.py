"""CompanyDocuments generator: Northwind rows -> canonical records -> themed PDF documents."""

import os
from pathlib import Path

# fontTools stamps "now" into every embedded font subset; pin it so reruns are byte-identical.
os.environ.setdefault("SOURCE_DATE_EPOCH", "0")

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
