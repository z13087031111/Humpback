"""Guard rail: no binary floats anywhere in the package source."""

import io
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "humpback"


def test_no_float_literals_or_float_calls() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tokens = list(tokenize.generate_tokens(io.StringIO(path.read_text()).readline))
        for i, tok in enumerate(tokens):
            if (
                tok.type == tokenize.NUMBER
                and any(c in tok.string for c in ".eE")
                and not (tok.string.lower().startswith("0x"))
            ):
                offenders.append(f"{path.name}:{tok.start[0]} literal {tok.string}")
            if (
                tok.type == tokenize.NAME
                and tok.string == "float"
                and i + 1 < len(tokens)
                and tokens[i + 1].string == "("
            ):
                offenders.append(f"{path.name}:{tok.start[0]} float() call")
    assert offenders == []
