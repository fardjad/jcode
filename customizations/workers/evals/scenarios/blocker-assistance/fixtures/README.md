# Parse readings

Repair `parser.py`. `parse` accepts a line containing a name and an integer
separated by `=`, with optional surrounding whitespace. It returns a pair
`(name, value)` and raises `ValueError` for missing names, separators, or
invalid integers. Run `python3 check.py` and `python3 -m unittest accept_test.py`.
