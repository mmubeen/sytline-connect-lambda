"""The repair item code: the save writes `R* <item>` (feature 004, FR-024)."""

import pytest

from syteline import rowbuild

LN = {"item": "PW1234", "quantity": 1, "uom": "EA"}


@pytest.mark.parametrize("item,expected", [
    ("PW1234", "R* PW1234"), ("  PW1234 ", "R* PW1234"), ("R* PW1234", "R* PW1234"), ("r*PW1234", "r*PW1234"),
    (" R* PW1234 ", "R* PW1234"), ("", ""), (None, "")])
def test_the_prefix_is_added_once_and_only_when_missing(item, expected):
    assert rowbuild.repair_item(item) == expected


def test_the_width_boundary():
    assert len(rowbuild.repair_item("A" * 27)) == 30 and rowbuild.item_too_long([{"item": "A" * 27}]) == []
    assert rowbuild.item_too_long([{"item": "A"}, {"item": "A" * 28}, {"item": "R* " + "A" * 28}]) == [2, 3]


def test_the_rows_hold_the_unpadded_repair_code_only_when_asked():
    assert rowbuild.co_line("100", 1, LN, "5000", repair=True)["item"] == "R* PW1234"
    assert rowbuild.job_row("100", 1, LN, "5000", {"po_number": "P"}, repair=True)["Item"] == "R* PW1234"
    assert rowbuild.co_line("100", 1, LN, "5000")["item"] == "PW1234".rjust(30)  # the one-step upload is unchanged
    assert rowbuild.job_row("100", 1, LN, "5000", {"po_number": "P"})["Item"] == "PW1234".rjust(30)
