import pytest

from mc2000b_ioc.protocol import (
    Blade, CommandError, ProtocolError, RefIn, RefOut, format_value,
    freq_resolution, parse_number, parse_reply, ref_in_choices, ref_out_choices,
)


def test_parse_query_reply():
    assert parse_reply("freq?", b"freq?\r1000\r> ") == "1000"


def test_parse_set_reply_empty():
    assert parse_reply("freq=10", b"freq=10\r> ") == ""


def test_parse_tolerates_crlf_and_leading_prompt():
    assert parse_reply("blade?", b"> blade?\r\n6\r\n> ") == "6"


def test_parse_command_error():
    with pytest.raises(CommandError):
        parse_reply("bogus?", b"bogus?\rCommand error CMD_NOT_DEFINED\r> ")


def test_parse_missing_prompt():
    with pytest.raises(ProtocolError):
        parse_reply("freq?", b"freq?\r10")


def test_parse_number():
    assert parse_number("1000") == 1000
    assert parse_number(" 20.5 ") == 20.5
    with pytest.raises(ProtocolError):
        parse_number("THORLABS")


@pytest.mark.parametrize("value,res,expected", [
    (100, 1.0, "100"), (100.4, 1.0, "100"), (20.04, 0.1, "20"),
    (20.06, 0.1, "20.1"), (4.123, 0.01, "4.12"), (5.0, 0.01, "5"),
])
def test_format_value(value, res, expected):
    assert format_value(value, res) == expected


def test_blade_tables():
    assert ref_in_choices(Blade.MC1F10HP)[3] is RefIn.EXT_INNER
    assert ref_in_choices(Blade.MC1F10) == (RefIn.INTERNAL, RefIn.EXTERNAL)
    assert ref_out_choices(Blade.MC2F57B)[4] is RefOut.DIFF
    assert ref_out_choices(Blade.MC1F60) == (RefOut.TARGET, RefOut.ACTUAL)
    assert freq_resolution(Blade.MC1F2) == 0.01
    assert freq_resolution(Blade.MC1F10HP) == 0.1
    assert freq_resolution(Blade.MC1F30) == 1.0
