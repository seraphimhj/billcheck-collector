import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class State:
    uidvalidity: str | None = None
    watermark: int = 0
    deferred: dict[str, float] = field(default_factory=dict)


def load(path: Path) -> State:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return State()
    return State(
        data.get("uidvalidity"),
        int(data.get("watermark", 0)),
        {str(k): float(v) for k, v in data.get("deferred", {}).items()},
    )


def save(path: Path, st: State) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(
                {"uidvalidity": st.uidvalidity, "watermark": st.watermark, "deferred": st.deferred},
                f,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
