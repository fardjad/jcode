"""Allow `python -m jcode_harness_client` to run the evaluation CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
