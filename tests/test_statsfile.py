"""Port of lox-datamanager tests/statfile.test.ts (parser side; LoxBridge only reads)."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from build_statfile import JAN_2024, build_statfile, make_records, stride_for

from loxbridge.miniserver.statsfile import UnsupportedStatsFile, lox_to_unix, parse_stats, stride_candidates

UTC = ZoneInfo("UTC")


def test_stride_models_agree_for_1_2_values_and_diverge_for_3_plus():
    assert stride_candidates(1) == [16]
    assert stride_candidates(2) == [32]
    assert stride_candidates(3) == [40, 32]
    assert stride_candidates(4) == [48, 64]
    assert stride_candidates(8) == [80, 88]


@pytest.mark.parametrize("vc,variant", [(1, "loxstatedit"), (2, "loxstatedit"), (3, "loxstatedit"), (3, "sarnau"),
                                        (4, "loxstatedit"), (4, "sarnau"), (8, "loxstatedit"), (8, "sarnau")])
def test_parses_both_stride_variants(vc, variant):
    recs = make_records(100, vc)
    name, buf = build_statfile(recs, vc, variant=variant)
    sf = parse_stats(buf, name, UTC)
    assert sf.stride == stride_for(vc, variant)
    assert len(sf.rows) == 100 and sf.uuid_mismatches == 0
    assert sf.rows[0] == (lox_to_unix(recs[0][0], UTC), recs[0][1])
    assert sf.control_type == 42


def test_detects_stride_when_size_divides_by_both():
    # 4 × 40 B (loxstatedit, vc=3) == 5 × 32 B (sarnau): uuid fragments and timestamps decide
    a = build_statfile(make_records(4, 3), 3, variant="loxstatedit")
    assert parse_stats(a[1], a[0], UTC).stride == 40
    b = build_statfile(make_records(5, 3), 3, variant="sarnau")
    assert parse_stats(b[1], b[0], UTC).stride == 32


def test_empty_single_record_utf8_and_suffix():
    for n in (0, 1):
        name, buf = build_statfile(make_records(n, 1), 1)
        assert len(parse_stats(buf, name, UTC).rows) == n
    name, buf = build_statfile(make_records(3, 1), 1, name="Küchen-Temperatur 🌡")
    assert parse_stats(buf, name, UTC).name == "Küchen-Temperatur 🌡"
    name, buf = build_statfile(make_records(5, 1), 1, suffix=2)
    assert name.endswith("_2.202401") and len(parse_stats(buf, name, UTC).rows) == 5


def test_rejects_garbage():
    with pytest.raises(UnsupportedStatsFile):
        parse_stats(bytes(4), "x.202401", UTC)
    huge = bytearray(64)
    huge[0:2] = (500).to_bytes(2, "little")
    with pytest.raises(UnsupportedStatsFile, match="value count"):
        parse_stats(bytes(huge), "10f7ca5e-01e3-15b2-ffff8caa2f4b3d5c.202401", UTC)
    name, buf = build_statfile(make_records(3, 1), 1)
    with pytest.raises(UnsupportedStatsFile, match="layout"):
        parse_stats(buf[:-3], name, UTC)


def test_uuid_mismatch_counted_and_nan_rows_dropped():
    name, buf = build_statfile(make_records(3, 1), 1, frags=(0xDEAD, 0xBEEF))
    assert parse_stats(buf, name, UTC).uuid_mismatches == 3
    name, buf = build_statfile([(JAN_2024, [1.0]), (JAN_2024 + 60, [float("nan")])], 1)
    assert len(parse_stats(buf, name, UTC).rows) == 1


def test_timestamps_are_miniserver_wall_clock():
    vienna = ZoneInfo("Europe/Vienna")
    assert lox_to_unix(JAN_2024, UTC) == 1704067200  # 2024-01-01T00:00Z
    assert lox_to_unix(JAN_2024, vienna) == 1704067200 - 3600


FIXTURES = sorted(p for p in (Path(__file__).parent / "fixtures").glob("*") if p.suffix[1:].isdigit())


@pytest.mark.skipif(not FIXTURES, reason="no real statistics files yet (add some from `check --dump`)")
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_real_fixture_files(path):
    sf = parse_stats(path.read_bytes(), path.name, UTC)
    assert sf.rows and sf.uuid_mismatches == 0
