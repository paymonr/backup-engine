"""Freeze guard for the validated cost-estimator math.

This is a regression SENTINEL, not a fail-first TDD test. It PASSES today
because the pins below are the SHA-256 hashes of the current, validated
estimator modules. It FAILS the moment anyone edits one of those frozen
files -- which is precisely its purpose: the UI redesign must not silently
change the math the estimator audit locked to ground truth.

If a change to one of these files is ever genuinely required, it must be
(a) deliberate, (b) re-validated against ground truth, and (c) accompanied
by an updated pin in this same file, in the same commit.
"""
import hashlib
from pathlib import Path

import pytest

ESTIMATOR_DIR = Path(__file__).resolve().parents[2] / "app" / "estimator"

# SHA-256 of the frozen files as of the redesign's first task. Do not edit a
# pin to make this test pass; edit it only alongside a deliberate, re-validated
# change to the corresponding file.
FROZEN_HASHES = {
    "model.py": "d1a6124e07d06bfd84be873cb5b12e819402e17aea57a7062e83fd59c0dd49aa",
    "tiered.py": "7d8d024c56a961fee7e807bed5991fd29a927a4b22f1f5da5a86de43579efa8e",
    "prices.py": "e22314cb8016406a8972ac2a33f61dcd7ea53b6974abcc6868e16ace78495e46",
    "usage.py": "958ba82392cbf9501804a760c0b0a62eb336cabe6ae07ac6a47c41708d349671",
    "billing.py": "06b1d8f47ebecde2dee6b46ed14ceb0b9a93a230090ef95755ec46b3913679b7",
    "schedule.py": "33fe88e211c534b88d657e26778a91a495313c4906dc0d7307b3fd9ef827d02c",
}


@pytest.mark.parametrize("filename, expected", sorted(FROZEN_HASHES.items()))
def test_estimator_file_is_untouched(filename, expected):
    actual = hashlib.sha256((ESTIMATOR_DIR / filename).read_bytes()).hexdigest()
    assert actual == expected, (
        f"{filename} changed: the frozen estimator math must not be edited by "
        f"the redesign. If this change is intentional and re-validated against "
        f"ground truth, update the pin in tests/estimator/test_untouched.py."
    )
