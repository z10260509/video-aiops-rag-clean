"""Explicit opt-in original-video checks; upload real jobs but never approve repairs."""

import os
from pathlib import Path
import pytest
from scripts.run_video_scenario import run

pytestmark = pytest.mark.skipif(
    os.getenv("VIDEO_V2_INTEGRATION") != "1",
    reason="Set VIDEO_V2_INTEGRATION=1 and VIDEO_SOURCE to explicitly upload test videos",
)


@pytest.mark.parametrize("fault", [False, True])
def test_original_video_to_playback_or_v2_review(tmp_path, fault):
    source = Path(os.environ["VIDEO_SOURCE"])
    result = run(
        source,
        tmp_path / ("fault.json" if fault else "healthy.json"),
        fault=fault,
        base=os.getenv("VIDEO_UI_URL", "http://127.0.0.1:13010"),
    )
    assert result["status"] == "passed" and result["operator_approval_submitted"] is False
    assert result["repair_executed"] is False
    if fault:
        assert result["report"]["validation_status"] == "validated"
        assert result["incident"]["lifecycle"] == "WAITING_APPROVAL"
    else:
        assert result["job"]["status"] == "COMPLETED" and result["playback_http"] == 206
