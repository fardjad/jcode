from parser import parse

try:
    assert parse(" temp = 12 ") == ("temp", 12)
except Exception:
    raise SystemExit("PARSER_CHECK_FAILURE: inspect separator handling; the command failed")
print("parser check passed")
