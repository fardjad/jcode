# Resume a batch

Complete `batch.py` to uppercase each nonempty line in `input.txt`, in order,
and write the records to `results.txt`. Use `checkpoint.json` to record the
number of input records already written. Repeated runs must not duplicate
records. `python3 batch.py` calls the supplied checkpoint helper after each
record. The helper can interrupt the process after a durable checkpoint.
Ignore empty input lines. Resume other input batches without duplicates.
Rerun the batch when needed, then run `python3 -m unittest accept_test.py`.
