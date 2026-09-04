"""The version constant must match the newest CHANGELOG entry."""

import pathlib
import re

import geekclock_claude as gc

ROOT = pathlib.Path(__file__).parent.parent


def test_version_matches_changelog():
    text = (ROOT / "CHANGELOG.md").read_text()
    newest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M).group(1)
    assert gc.__version__ == newest
