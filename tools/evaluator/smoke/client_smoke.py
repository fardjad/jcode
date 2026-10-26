"""Send one prompt through the real API bridge and assert the fake response."""

from __future__ import annotations

import os

from jcode_harness_client import HarnessClient


def main() -> None:
    working_dir = os.environ["JCODE_SMOKE_WORKING_DIR"]
    with HarnessClient.tcp(
        host=os.environ["JCODE_API_HOST"],
        port=int(os.environ["JCODE_API_PORT"]),
        turn_timeout=60,
    ) as client:
        session = client.create_session(working_dir=working_dir)
        result = session.send_message(
            "Reply with exactly the words: smoke reply. Do not use any tools."
        )
        if not result.completed_normally:
            raise SystemExit(
                f"expected normal completion, got stop {result.stop_reason}: "
                f"{result.stop_message}"
            )
        if result.text.strip() != "smoke reply":
            raise SystemExit(f"expected 'smoke reply', got {result.text!r}")
        print(result.as_dict())


if __name__ == "__main__":
    main()
