# Normalize labels

Complete `normalize.py` to read `input.csv` and write `output.csv`, preserving
the header and row order. Strip whitespace from labels and lowercase them.
Handle other rows and quoted CSV fields too.
Run `python3 check.py` and `python3 -m unittest accept_test.py`. The check
uses a local service probe that can temporarily fail.
