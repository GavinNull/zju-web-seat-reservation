from pathlib import Path


def test_detection_badge_requires_active_task_state() -> None:
    script = Path("src/seat_assistant/static/app.js").read_text(encoding="utf-8")

    assert 'const activeStates = ["running", "submitting", "verifying"];' in script
    assert "activeStates.includes(task.state)" in script
