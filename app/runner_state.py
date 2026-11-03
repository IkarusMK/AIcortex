"""What each runner did last, for the WebUI — kept in memory, so a restart shows
"waiting" until the next minute."""
import threading
from datetime import datetime, timezone

_states: dict = {}
_lock = threading.Lock()
IDLE = {"state": "waiting", "detail": "", "link": "", "at": ""}


def put(rid: str, state: str, detail: str = "", link: str = "") -> None:
    """state: idle | ran | no-key | no-signin | error."""
    with _lock:
        _states[rid] = {"state": state, "detail": str(detail)[:200], "link": link,
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def get(rid: str, enabled: bool) -> dict:
    if not enabled:
        return dict(IDLE, state="off")
    with _lock:
        return dict(_states.get(rid) or IDLE)


def clear() -> None:
    with _lock:
        _states.clear()
