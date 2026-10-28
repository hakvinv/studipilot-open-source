#!/usr/bin/env python3
"""Run offline suites without production credentials or state."""
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
for folder, modules in [
    ('railway-studip-sync', ['test_sync', 'test_rag', 'test_pipeline']),
    ('uni-rag', ['test_app', 'test_embeddings']),
    ('tests', ['test_skill']),
    ('remote-rag', ['test_remote']),
    ('uni-mail', ['test_mail', 'test_pilot_state']),
    ('uni-gateway', ['test_gateway', 'test_calendar_sync']),
    ('uni-pilot', ['test_pilot']),
]:
    result = subprocess.run([sys.executable, '-m', 'unittest', '-v', *modules], cwd=ROOT/folder)
    if result.returncode:
        sys.exit(result.returncode)
