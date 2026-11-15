from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def test_dashboard_renders_every_tab_and_runs_a_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "dashboard.log"))
    monkeypatch.setenv("LOCAL_MEDIA_DIR", str(tmp_path / "media"))

    app = AppTest.from_file(str(APP), default_timeout=120).run()

    assert not app.exception
    assert [tab.label for tab in app.tabs][:3] == ["Pipelines", "KPIs & Budget", "Queue & Logs"]
    assert any(metric.label == "Net profit (USD)" for metric in app.metric)
    assert any("Monthly AI spend" in header.value for header in app.subheader)

    app.button(key="run_sports_he").click().run()

    assert not app.exception
    assert any("Dry-run result" in expander.label for expander in app.expander)
    assert any("לרכישה" in block.value or "הזמנות" in block.value for block in app.markdown)
