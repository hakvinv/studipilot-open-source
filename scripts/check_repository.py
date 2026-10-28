#!/usr/bin/env python3
"""Reject private runtime artifacts and recognizable credential values before publication."""
import hashlib
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
IGNORE = {'.git', '.venv', '__pycache__'}
FIXTURE_SHA = 'f18bc5384527e730a1544994c30c15d80b435122151bb5fbafcbbb32c3e151f5'
patterns = [
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(rb'github_pat_[A-Za-z0-9_]{30,}'),
    re.compile(rb'sk-(?:proj-)?[A-Za-z0-9_-]{30,}'),
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'["\'](?:access_token|refresh_token|api_key)["\']\s*:\s*["\'][^"\'\n]{25,}["\']', re.I),
]
errors = []
count = 0
for path in sorted(ROOT.rglob('*')):
    rel = path.relative_to(ROOT)
    if any(part in IGNORE for part in rel.parts):
        continue
    if path.is_symlink():
        errors.append(str(rel)+': symlink'); continue
    if not path.is_file():
        continue
    if path.suffix == '.pyc':
        continue
    count += 1
    data = path.read_bytes()
    name = path.name.lower()
    if name.startswith('.env') or name in {'rclone.conf','embedding-private.json','mail-private.json','remote-access.json'} or path.suffix.lower() in {'.conf','.sqlite','.db','.log','.xlsx','.pptx','.docx','.pem','.key'}:
        errors.append(str(rel)+': runtime/secret/material artifact')
    if path.suffix.lower() == '.pdf':
        if rel.as_posix() != 'tests/fixtures/course-sample.pdf' or hashlib.sha256(data).hexdigest() != FIXTURE_SHA:
            errors.append(str(rel)+': unexpected PDF')
    if any(pattern.search(data) for pattern in patterns):
        errors.append(str(rel)+': credential signature detected (value withheld)')
if errors:
    print('\n'.join(errors),file=sys.stderr)
    sys.exit(1)
print(f'Repository check passed: {count} files; no forbidden runtime artifacts or recognized credential values.')
