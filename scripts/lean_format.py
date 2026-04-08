#!/usr/bin/env python3
"""Format Lean 4 code by compiling and checking it, extracting any formatting.

Since Lean 4's built-in PrettyPrinter.formatCommand requires running
inside a Lean environment (CoreM monad), the simplest approach that
works is to use `lean --run` with a formatting script.

For now, this does a simpler but effective approach: parse the tactic
block structure and re-indent using Lean 4 rules:
- After `:= by`, indent contents by 2
- `have`/`let`/`show`/etc. at the same level are siblings
- Contents of inner `by` blocks get +2
- `·` bullets align with their parent tactic
"""
import re
import sys

_TACTIC_START = re.compile(
    r"^(have|let|show|suffices|obtain|intro|apply|exact|rw|simp|"
    r"cases|rcases|induction|by_cases|constructor|use|refine|"
    r"calc|match|omega|linarith|ring|norm_num|aesop|trivial|"
    r"decide|tauto|sorry|haveI|letI)\b"
)


def format_lean_source(source: str) -> str:
    """Format a complete Lean 4 source file with correct indentation."""
    lines = source.split("\n")
    result: list[str] = []
    in_by_block = False
    by_depth = 0  # how many nested `by` blocks deep

    # Track block openers: list of (depth, original_indent, content_indent)
    # content_indent = indent of the first line INSIDE this by block (0 if unknown)
    by_stack: list[tuple[int, int, int]] = []

    for line in lines:
        stripped = line.strip()

        # Pass through blank lines
        if not stripped:
            result.append("")
            continue

        original_indent = len(line) - len(line.lstrip())

        # Lines before `:= by` — pass through unchanged (imports, theorem declaration)
        if not in_by_block:
            if stripped.endswith(":= by") or stripped.endswith("by"):
                in_by_block = True
                by_depth = 0
                by_stack = [(0, original_indent, 0)]
                result.append(line)
            else:
                result.append(line)
            continue

        # Inside a `by` block — re-indent
        is_tactic = bool(_TACTIC_START.match(stripped))
        is_bullet = stripped.startswith("·") or stripped.startswith("| ")

        # Pop blocks: if this tactic's original indent is less than the
        # content indent of the current by block, it belongs to a parent
        if is_tactic or is_bullet:
            while len(by_stack) > 1:
                _, opener_indent, content_indent = by_stack[-1]
                if content_indent > 0 and original_indent < content_indent:
                    by_stack.pop()
                elif content_indent == 0:
                    # Haven't seen content yet — don't pop, this IS the content
                    break
                else:
                    break

        # Determine output depth
        current_depth = by_stack[-1][0] if by_stack else 0

        if is_tactic or is_bullet:
            output_indent = 2 + current_depth * 2
        else:
            # Continuation line — one deeper
            output_indent = 2 + (current_depth + 1) * 2

        result.append(" " * output_indent + stripped)

        # Track the content indent of the current block (first line seen inside it)
        if by_stack and by_stack[-1][2] == 0 and not stripped.endswith(" by") and not stripped.endswith(":= by"):
            depth_val, opener_val, _ = by_stack[-1]
            by_stack[-1] = (depth_val, opener_val, original_indent)

        # Push if this line opens a new `by` block
        if stripped.endswith(" by") or stripped.endswith(":= by"):
            by_stack.append((current_depth + 1, original_indent, 0))

    return "\n".join(result)


def main():
    if len(sys.argv) < 2:
        # Read from stdin
        source = sys.stdin.read()
    else:
        source = open(sys.argv[1]).read()

    formatted = format_lean_source(source)
    print(formatted)


if __name__ == "__main__":
    main()
