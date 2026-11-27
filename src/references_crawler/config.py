"""References crawler configuration.

Timing and batch settings can be overridden with --cycle, --wait-429, and --batch-size.
"""

import os

# =====================================================================
# Request pacing
# =====================================================================
CYCLE_SECONDS = 1  # Minimum interval between request starts.
WAIT_429_SECONDS = (
    5  # Base cooldown before retrying the same request after HTTP 429 (retries 1-2).
)
WAIT_429_STEP_SECONDS = (
    5  # Extra cooldown added per retry from the 3rd HTTP 429 retry onward.
)

# =====================================================================
#  BATCH
# =====================================================================
BATCH_SIZE = 200  # Publications per batch; the endpoint accepts up to 500.
FALLBACK_BATCH_SIZE = 50  # Sub-batch size when a larger batch exhausts retries.

# =====================================================================
# Retry limits
# =====================================================================
MAX_429_RETRIES = 8  # Maximum HTTP 429 retries per request.
FALLBACK_429_WAITS = (40, 50, 60)  # HTTP 429 retry waits for fallback 50 sub-batches.
FINAL_RETRY_429_WAITS = (20, 40, 60)  # HTTP 429 retry waits for end-of-run deferred pass.
MAX_OTHER_RETRIES = 3  # Maximum retries for network errors or HTTP 5xx.
REQUEST_TIMEOUT = 120  # Seconds.

# =====================================================================
# Completeness verification
# Lists at or above this size are checked against the paged references endpoint.
# This is a conservative trigger, not a known Semantic Scholar limit.
# =====================================================================
TRUNCATION_CHECK_MIN_REFS = 500

# Keep the compatibility name for existing imports. Count checks always apply;
# setting this to False does not make a mismatched snapshot complete.
VERIFY_ON_COUNT_MISMATCH = True

# =====================================================================
# Measurement
# =====================================================================
RAM_SAMPLE_INTERVAL = 0.5  # Seconds between RAM samples.

# =====================================================================
# API key names, loaded from the environment or a local .env file.
# =====================================================================
KEY_NAMES = ("S2_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "SEMANTIC_SCHOLAR_KEY")


def load_env():
    """Load local .env values without replacing existing environment variables."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.removeprefix("export ").strip()
                os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def get_api_key():
    for name in KEY_NAMES:
        if os.environ.get(name):
            return os.environ[name]
    return None
