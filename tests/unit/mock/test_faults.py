"""T026: every fault of the catalogue has a place that implements it, and applies to the right stand-in."""

import re

from edge import faults, models

CONTRACT = "specs/008-production-like-mock-env/contracts/control-api.md"


def test_every_catalogue_fault_has_a_handler_and_none_is_extra():
    assert set(faults.HANDLED_BY) == set(models.CATALOGUE)


def test_the_target_matches_the_prefix_and_the_handler():
    for fid, target in models.CATALOGUE.items():
        handler = faults.HANDLED_BY[fid]
        if fid.startswith(("erp.", "sql.")):
            assert target == handler == fid.split(".")[0]
        else:
            assert target == "aws" and handler in ("edge", "world")


def test_the_contract_lists_exactly_the_catalogue():
    import os

    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    text = open(os.path.join(root, CONTRACT)).read()
    listed = set(re.findall(r"`((?:idp|erp|sql|secrets|lock|lambda)\.[a-z_]+)`", text.split("Fault catalogue")[1]))
    assert listed == set(models.CATALOGUE)
