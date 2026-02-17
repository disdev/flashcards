#!/usr/bin/env python3
"""
Extract multiple-choice questions from MTC/DGAC aviation exam PDFs
and output them as JSON compatible with the flashcard app.

Requirements:
  - Python 3.8+
  - pdftotext (from poppler): brew install poppler

Usage:
  # Extract to stdout (inspect / pipe)
  python3 extract_questions.py "path/to/exam.pdf"

  # Extract and append into cards.json
  python3 extract_questions.py "path/to/exam.pdf" --merge cards.json

  # Override the auto-detected category
  python3 extract_questions.py "path/to/exam.pdf" --category "Mi Categoría"
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def pdf_to_text(pdf_path: str) -> str:
    """Convert a PDF to text using pdftotext with layout preservation."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["pdftotext", "-layout", pdf_path, tmp_path],
            check=True,
            capture_output=True,
        )
        return Path(tmp_path).read_text(encoding="utf-8")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def detect_category(text: str) -> str:
    """Try to pull the TEMA line from the header to use as category."""
    m = re.search(r"TEMA:\s+\S+\s+(.+?)(?:\s*-\s*PP\b.*)?$", text, re.MULTILINE)
    if m:
        # Clean up: strip trailing year, whitespace
        cat = re.sub(r"\s+\d{4}\s*$", "", m.group(1).strip())
        return cat
    return "Sin categoría"


def parse_questions(text: str, category: str) -> list[dict]:
    """
    Parse the structured MTC exam text into a list of flashcard dicts.

    Each question block looks like:
        PREG<code>  <number>.- <question text>           <answer letter>
        <code cont> <question text cont>
        OPCION A:   <text>
        OPCION B:   <text>
        OPCION C:   <text>

    The answer letter (A/B/C) appears at the end of the PREG line.
    """
    # ── Step 1: strip page headers so they don't pollute question text ──
    # Headers contain "DIRECCION DE PERSONAL AERONAUTICO", page numbers, etc.
    header_re = re.compile(
        r"^\s*(?:DIRECCION DE PERSONAL|MTC\s|OGMS/DINF|"
        r"TEMA:|COD PREG:|\s*Pag:).*$",
        re.MULTILINE,
    )
    text = header_re.sub("", text)

    # Also strip date/time stamps that appear on their own
    text = re.sub(r"^\s*\d{2}/\d{2}/\d{4}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d{2}:\d{2}\s*$", "", text, flags=re.MULTILINE)

    # ── Step 2: locate each question block ──
    # A question starts with a PREG code line that contains the question number
    # like "3248.-" and ends just before the next PREG line (or EOF).
    # The answer letter (A, B, or C) sits at the far right of the PREG line(s).

    # We'll split by PREG boundaries first.
    # Pattern: PREG followed by digits, then the rest of the block.
    blocks = re.split(r"(?=PREG\d{11,})", text)

    cards = []

    for block in blocks:
        block = block.strip()
        if not block or not block.startswith("PREG"):
            continue

        # ── Extract the answer letter ──
        # It appears as a standalone A/B/C/D at the end of one of the first
        # lines (before OPCION). Grab it case-insensitively.
        pre_options = block.split("OPCION")[0] if "OPCION" in block else block
        answer_match = re.search(r"\b([A-Da-d])\s*$", pre_options, re.MULTILINE)
        if not answer_match:
            # Try the very end of the first two lines
            first_lines = pre_options.strip().splitlines()[:3]
            for ln in first_lines:
                m = re.search(r"\b([A-Da-d])\s*$", ln.strip())
                if m:
                    answer_match = m
                    break
        if not answer_match:
            print(f"⚠  Could not find answer letter in block, skipping:\n{block[:120]}…", file=sys.stderr)
            continue

        answer_letter = answer_match.group(1).upper()

        # ── Strip the PREG header lines from the block ──
        # The PREG code spans two lines in the PDF layout, e.g.:
        #   PREG20241107009 3248.- question text ...       c
        #   3               continuation of question ...
        # The first line has the PREG code prefix + question start + answer letter.
        # The second line starts with the trailing digit(s) of the PREG code,
        # followed by optional whitespace and question text continuation.
        # We need to:
        #  1. Remove the "PREG..." prefix from line 1
        #  2. Remove the trailing PREG digit(s) from line 2
        lines = block.splitlines()
        # Line 0: "PREG20241107009 3248.- question text ...   c"
        # Strip the PREG code prefix, keeping from the question number onward
        if lines:
            lines[0] = re.sub(r"^PREG\d+\s+", "", lines[0])
        # Line 1 (if present): starts with 1-2 trailing digits of the PREG code.
        # Two cases:
        #   a) digit(s) + lots of space + question continuation text
        #   b) digit(s) alone on the line (short questions that fit on line 0)
        if len(lines) > 1:
            stripped = lines[1].strip()
            if re.match(r"^\d{1,2}$", stripped):
                # Case b: line is just the trailing PREG digit — remove entirely
                lines[1] = ""
            else:
                # Case a: strip the leading digit(s) + whitespace padding
                lines[1] = re.sub(r"^\s{0,2}\d{1,2}\s{2,}", "", lines[1])
        block_clean = "\n".join(lines)

        # ── Extract question text ──
        # Sits between the question number (e.g. "3248.-") and the first OPCION.
        # Some PDFs use "3828." instead of "3828.-", so we accept both.
        q_match = re.search(
            r"(\d[\d.]+)\.[-\s]\s*(.*?)(?=OPCION\s+[A-D]:)",
            block_clean,
            re.DOTALL,
        )
        if not q_match:
            print(f"⚠  Could not parse question text in block, skipping:\n{block[:120]}…", file=sys.stderr)
            continue

        q_number = q_match.group(1).strip().rstrip(".")
        q_text_raw = q_match.group(2)

        # Remove the answer letter that was embedded in the question area
        q_text_raw = re.sub(r"\b[A-Da-d]\s*$", "", q_text_raw, flags=re.MULTILINE)

        # Collapse internal whitespace / line breaks into single spaces
        q_text = " ".join(q_text_raw.split()).strip()

        question = f"{q_number}.- {q_text}"

        # ── Extract options ──
        options_raw = re.findall(
            r"OPCION\s+([A-D]):\s*(.*?)(?=OPCION\s+[A-D]:|PREG\d|$)",
            block_clean,
            re.DOTALL,
        )
        if len(options_raw) < 2:
            print(f"⚠  Found fewer than 2 options for question {q_number}, skipping.", file=sys.stderr)
            continue

        options = []
        letter_to_index = {}
        for i, (letter, opt_text) in enumerate(options_raw):
            cleaned = " ".join(opt_text.split()).strip()
            options.append(cleaned)
            letter_to_index[letter.upper()] = i

        if answer_letter not in letter_to_index:
            print(f"⚠  Answer letter '{answer_letter}' not in options for {q_number}, skipping.", file=sys.stderr)
            continue

        answer_index = letter_to_index[answer_letter]

        cards.append({
            "category": category,
            "question": question,
            "options": options,
            "answer": answer_index,
        })

    return cards


def main():
    parser = argparse.ArgumentParser(
        description="Extract MTC aviation exam questions from PDF into flashcard JSON."
    )
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument(
        "--merge",
        metavar="JSON_FILE",
        help="Append extracted cards to an existing JSON file instead of printing to stdout",
    )
    parser.add_argument(
        "--category",
        help="Override the auto-detected category name",
    )
    args = parser.parse_args()

    pdf_path = args.pdf
    if not Path(pdf_path).exists():
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"📄 Extracting text from: {pdf_path}", file=sys.stderr)
    text = pdf_to_text(pdf_path)

    category = args.category or detect_category(text)
    print(f"📂 Category: {category}", file=sys.stderr)

    cards = parse_questions(text, category)
    print(f"✅ Extracted {len(cards)} questions", file=sys.stderr)

    if not cards:
        print("No questions extracted.", file=sys.stderr)
        sys.exit(1)

    # Validate
    for i, c in enumerate(cards):
        assert c["answer"] < len(c["options"]), (
            f"Card {i} ({c['question'][:40]}…): answer index {c['answer']} "
            f"out of range for {len(c['options'])} options"
        )

    if args.merge:
        merge_path = Path(args.merge)
        if merge_path.exists():
            existing = json.loads(merge_path.read_text(encoding="utf-8"))
        else:
            existing = []

        # Deduplicate by question text
        existing_questions = {c["question"] for c in existing}
        new_cards = [c for c in cards if c["question"] not in existing_questions]
        dupes = len(cards) - len(new_cards)

        merged = existing + new_cards
        merge_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"💾 Merged into {args.merge}: {len(new_cards)} new, {dupes} duplicates skipped, {len(merged)} total",
            file=sys.stderr,
        )
    else:
        print(json.dumps(cards, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
