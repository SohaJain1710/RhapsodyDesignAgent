"""
debug_logger.py
---------------
Full debug logging — no truncation anywhere.
Shows complete input, output, prompts and responses at each step.
"""
import os
import json
import sys
from datetime import datetime

from config import RUNTIME_DIR


class DebugLogger:
    def __init__(self, component: str):
        self.component = component
        self.log_file  = os.path.join(RUNTIME_DIR, f"_debug_{component}.log")
        self.json_file = os.path.join(RUNTIME_DIR, f"_debug_{component}.json")
        self.entries   = []

        # Always append — don't clear existing log
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"=== RhapsodyAIAgent Debug Log ===\n")
            f.write(f"Component: {component}\n")
            f.write(f"Started  : {datetime.now().isoformat()}\n")
            f.write(f"{'='*60}\n\n")

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S")

    def _write(self, tag: str, title: str, content: str = ""):
        line = f"[{self._ts()}] [{tag}] {title}"
        print(line, file=sys.stderr)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if content:
                f.write(content + "\n")
            f.write("\n")
        self.entries.append({
            "ts": self._ts(), "tag": tag,
            "title": title, "content": content,
        })
        self._save_json()

    def _save_json(self):
        with open(self.json_file, "w", encoding="utf-8") as f:
            json.dump({"component": self.component,
                       "entries": self.entries}, f, indent=2, ensure_ascii=False)

    def step(self, node: str, message: str):
        """Log a pipeline step."""
        self._write("STEP", f"{node}: {message}")

    def data(self, label: str, data):
        """Log full data — no truncation."""
        if isinstance(data, (list, dict)):
            content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        else:
            content = str(data)
        count = f" ({len(data)} items)" if isinstance(data, list) else ""
        self._write("DATA", f"{label}{count}", content)

    def llm_prompt(self, phase: str, prompt: str):
        """Log full prompt sent to LLM — no truncation."""
        self._write("LLM→", f"Phase={phase} | {len(prompt)} chars", prompt)

    def llm_response(self, phase: str, response: str):
        """Log full LLM response — no truncation."""
        self._write("←LLM", f"Phase={phase} | {len(str(response))} chars",
                    str(response))

    def summary(self, node: str, stats: dict):
        """Log node summary."""
        content = "\n".join(f"  {k}: {v}" for k, v in stats.items())
        self._write("SUMMARY", node, content)

    def error(self, node: str, error: str):
        """Log an error."""
        self._write("ERROR", f"{node}: {error}")

    def separator(self, label: str = ""):
        line = f"\n{'─'*50} {label} {'─'*50}\n"
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line)
        print(line, file=sys.stderr)
