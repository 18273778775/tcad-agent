# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

"""Private one-request worker; the public CLI supervises time, memory and output."""

import json
import sys

from .errors import CadError
from .protocol import MAX_JSON_BYTES, execute_request, loads_json
from .output import bounded_failure


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace", newline="\n")
    try:
        raw = sys.stdin.read(MAX_JSON_BYTES + 1)
        response = execute_request(loads_json(raw))
    except CadError as exc:
        response = bounded_failure(exc)
    print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
