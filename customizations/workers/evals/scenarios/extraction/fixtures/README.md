# Inventory summary

Complete `aggregate.py` to read the text files in `facts/` and write
`summary.json`. Each line is a `name: color` fact. Return a JSON object mapping
each name to its color. Repeated identical facts should appear only once.
The program must also handle new fact files and names.
Run `python3 aggregate.py` and `python3 -m unittest accept_test.py`.
