"""Opt-in process-tree measurement, used only by measure_cli_panel.py."""
import os

if os.environ.get('BRAIN_PANEL_METRICS'):
    import metrics
    metrics.install()
