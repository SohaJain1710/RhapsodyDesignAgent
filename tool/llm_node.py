"""
llm_node.py
-----------
LLM relay via file-based queue.

Instead of calling the VS Code proxy directly (which fails with 403
outside the chat handler), Python writes prompts to a request file
and waits for JS to write the response.

JS handler stays open, polls for requests, calls LLM with valid token,
writes response back.

File protocol:
  RUNTIME_DIR/llm_request.json  <- Python writes prompt here
  RUNTIME_DIR/llm_response.json <- JS writes response here
  RUNTIME_DIR/llm_error.json    <- JS writes error here
"""
from __future__ import annotations
import json
import os
import re
import time

from config import RUNTIME_DIR
REQUEST_FILE = os.path.join(RUNTIME_DIR, "llm_request.json")
RESPONSE_FILE= os.path.join(RUNTIME_DIR, "llm_response.json")
ERROR_FILE   = os.path.join(RUNTIME_DIR, "llm_error.json")
POLL_INTERVAL= 0.5    # seconds between polls
DEFAULT_TIMEOUT = 500  # seconds


def llm_call(
    prompt: str,
    system: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    fallback_interrupt: dict | None = None,
) -> str:
    """
    Write prompt to request file, wait for JS to write response.
    JS handler polls request file, calls LLM, writes response file.
    """
    # Clean up old files
    for f in [REQUEST_FILE, RESPONSE_FILE, ERROR_FILE]:
        try: os.remove(f)
        except: pass

    # Write request
    request = {
        "prompt": prompt,
        "system": system,
        "ts"    : time.time(),
    }
    with open(REQUEST_FILE, "w", encoding="utf-8") as f:
        json.dump(request, f)

    print(f"[LLMNode] Waiting for JS relay... (timeout={timeout}s)")

    # Poll for response
    deadline = time.time() + timeout
    while time.time() < deadline:
        # Check for error
        if os.path.exists(ERROR_FILE):
            with open(ERROR_FILE, encoding="utf-8") as f:
                err = json.load(f)
            try: os.remove(ERROR_FILE)
            except: pass
            msg = err.get("error", "Unknown LLM error")
            print(f"[LLMNode] JS relay error: {msg}")
            if fallback_interrupt is not None:
                from langgraph.types import interrupt
                return interrupt(fallback_interrupt)
            raise RuntimeError(f"LLM relay error: {msg}")

        # Check for response
        if os.path.exists(RESPONSE_FILE):
            with open(RESPONSE_FILE, encoding="utf-8") as f:
                resp = json.load(f)
            try: os.remove(RESPONSE_FILE)
            except: pass
            content = resp.get("content", "")
            print(f"[LLMNode] Got response: {len(content)} chars")
            return content

        time.sleep(POLL_INTERVAL)

    # Timeout
    print(f"[LLMNode] Timeout after {timeout}s")
    try: os.remove(REQUEST_FILE)
    except: pass
    if fallback_interrupt is not None:
        from langgraph.types import interrupt
        return interrupt(fallback_interrupt)
    raise RuntimeError(f"LLM relay timeout after {timeout}s")


def extract_json(text: str, default: dict | None = None) -> dict:
    """Extract first JSON object from LLM response."""
    if not isinstance(text, str):
        return default if default is not None else {}

    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    start = cleaned.find("{")
    if start == -1:
        print(f"[LLMNode] No JSON object found in response: {cleaned[:100]!r}")
        return default if default is not None else {}

    depth = 0
    for i, ch in enumerate(cleaned[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start:i+1])
                except json.JSONDecodeError:
                    pass
    return default if default is not None else {}


def llm_json(
    prompt: str,
    system: str | None = None,
    default: dict | None = None,
    **kwargs,
) -> dict:
    """Call LLM and parse JSON from response."""
    text = llm_call(prompt, system=system, **kwargs)
    return extract_json(text, default=default)


if __name__ == "__main__":
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hello in one word"
    print(llm_call(prompt))
