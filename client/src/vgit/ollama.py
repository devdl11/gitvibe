"""JSON stdin/stdout adapter: python -m vgit.ollama MODEL."""

import argparse
import json
import sys
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--url", default="http://localhost:11434/api/chat")
    args = parser.parse_args()
    request = json.load(sys.stdin)
    instructions = request.pop("instructions")
    payload = {
        "model": args.model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(request)},
        ],
    }
    try:
        with urlopen(Request(args.url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}), timeout=280) as response:
            result = json.load(response)
        value = json.loads(result["message"]["content"])
        json.dump(value, sys.stdout)
    except Exception as error:
        print(f"Ollama adapter failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
