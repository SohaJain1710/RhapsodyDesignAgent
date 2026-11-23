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


def _json_candidates(text: str):
    """Yield balanced {...} substrings, ignoring braces inside JSON strings."""
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _lenient(s: str) -> str:
    """Remove // and /* */ comments and trailing commas (common LLM slips)."""
    s = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
               lambda m: m.group(1) or "", s, flags=re.S)
    return re.sub(r",\s*([}\]])", r"\1", s)


def extract_json(text: str, default: dict | None = None) -> dict:
    """Extract the first (largest parseable) JSON object from an LLM response.
    Sets extract_json.last_error when nothing could be parsed."""
    extract_json.last_error = ""
    if not isinstance(text, str):
        extract_json.last_error = "LLM response is not text"
        return default if default is not None else {}
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    if "{" not in cleaned:
        extract_json.last_error = f"no JSON object in LLM response: {cleaned[:120]!r}"
        print(f"[LLMNode] {extract_json.last_error}")
        return default if default is not None else {}
    for cand in _json_candidates(cleaned):
        for variant in (cand, _lenient(cand)):
            try:
                obj = json.loads(variant)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    extract_json.last_error = f"LLM response could not be parsed as JSON: {cleaned[:120]!r}"
    print(f"[LLMNode] {extract_json.last_error}")
    return default if default is not None else {}


extract_json.last_error = ""


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
