"""
Pipeline views package.

Each file handles one feature area. Import all view functions here
so urls.py can reference them as `views.function_name`.

PARALLEL DEVELOPMENT RULES:
- Each file is owned by one developer/chat at a time
- Do NOT modify __init__.py imports for other people's files
- Do NOT modify models.py — it is frozen until Priority 2
- Create your templates in templates/pipeline/your_feature/
- All queries must use .using('pipeline_db')
"""

# === Priority 1: Views on existing data ===
from pipeline.views.hub import *             # Task-first front door
from pipeline.views.dashboard import *       # Target dashboard + detail
from pipeline.views.search import *          # Antibody search + cell line list

# === Priority 2: Feasibility & entry ===
from pipeline.views.feasibility import *     # API-powered feasibility lookup
from pipeline.views.session_entry import *   # Experiment session creation/editing
from pipeline.views.session_bulk import *    # Plan a session by paste/upload + preview

# === Priority 3: Automation ===
from pipeline.views.cropper import *          # Figure cropper (grid → crops → review queue)
from pipeline.views.review import *           # Review queue: release crops to the public site
from pipeline.views.bulk_import import *       # Bulk antibody paste (add/update many)
from pipeline.views.imports import *           # xlsx/csv templates + upload
from pipeline.views.deletion import *      # Delete one record, typed confirmation
from pipeline.views.attachments import *   # Raw files against a session
from pipeline.views.deposit import *       # Deposit a gene's data to Zenodo
from pipeline.views.data_io import *           # Whole-dataset download / upsert upload
from pipeline.views.recommendations import *   # Visual gene-at-a-time recommendation manager
from pipeline.views.target_board import *      # Cross-site target board + portfolio
from pipeline.views.session_board import *     # Sessions board (find + edit in place)
from pipeline.views.antibody_board import *    # Antibodies board (find + edit in place)
from pipeline.views.cell_line_board import *   # Cell lines board (find + edit in place)
from pipeline.views.guides import *            # Board guides (one Markdown file each)
from pipeline.views.find import *              # The one search box, and where it lands
from pipeline.views.user_admin import *        # People board (logins + access), superusers only
from pipeline.views.impact import *          # Impact metrics across all four apps, superusers only
# from pipeline.views.reports import *
# from pipeline.views.receiving import *
