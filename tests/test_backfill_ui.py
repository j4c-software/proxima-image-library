from pathlib import Path


def test_maintenance_has_selective_backfill_controls_and_live_counters():
    template = (Path(__file__).parents[1] / "templates" / "maintenance.html").read_text()

    for marker in (
        'id="backfill-scan-btn"',
        'class="backfill-row-check"',
        'id="backfill-select-visible-btn"',
        'id="backfill-submit-btn"',
        'id="backfill-count-elapsed"',
        'id="backfill-count-processed"',
        'id="backfill-count-succeeded"',
        'id="backfill-count-failed"',
        "/api/maintenance/hero-backfill/run",
        "response.body.getReader()",
    ):
        assert marker in template
