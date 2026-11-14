# Two parsing stages

Repair `stages.py`. `split_record` splits a `name|quantity` record and rejects
missing or extra separators. `clean_record` trims both fields, rejects an empty
name and returns the name and integer quantity. Run `python3 check.py` and
`python3 -m unittest accept_test.py`.
