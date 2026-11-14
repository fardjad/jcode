from stages import clean_record, split_record

try:
    parts = split_record(" pear | 4 ")
    if len(parts) != 2:
        raise ValueError("stage one")
except Exception:
    raise SystemExit("STAGE_ONE_FAILURE: check the delimiter")
try:
    assert clean_record(parts) == ("pear", 4)
except Exception:
    raise SystemExit("STAGE_TWO_FAILURE: check whitespace and numeric conversion")
print("both stages passed")
