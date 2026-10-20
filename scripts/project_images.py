"""The complete Kustomization is the single source of image versions (JSON is valid YAML)."""

import json
from pathlib import Path

_CONFIG = json.loads((Path(__file__).resolve().parents[1] / "k8s/kustomization.yaml").read_text())
_IMAGES = {
    row["name"]: row.get("newName", row["name"]) + ":" + row["newTag"] for row in _CONFIG["images"]
}
CONTROL = _IMAGES["video-platform-aiops"]
VIDEO = _IMAGES["video-platform-worker"]
UI = _IMAGES["video-platform-aiops-ui"]
DATABASE = _IMAGES["video-platform-aiops-db"]
