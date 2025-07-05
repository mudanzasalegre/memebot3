# utils/time.py
from datetime import datetime, timezone
def utc_now():
    return datetime.now(timezone.utc)
