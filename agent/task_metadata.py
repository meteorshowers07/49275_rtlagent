"""Evaluation metadata inferred only from the public task interface."""
import re


def detect_clock_port(prompt):
    """Return a unique scalar clock input, or None for ambiguous interfaces."""
    ports = re.findall(r"(?m)^\s*-\s*input\s+(?:(?:wire|logic|reg)\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b", prompt)
    candidates = [port for port in ports
                  if re.fullmatch(r"(?i)(?:(?:[a-z0-9]+_)*)(?:clk|clock)(?:_[a-z0-9]+)*", port)]
    return candidates[0] if len(candidates) == 1 else None
