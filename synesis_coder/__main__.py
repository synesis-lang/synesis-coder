"""Entry point para invocação via `python -m synesis_coder`."""

import sys

# Força UTF-8 em stdin/stdout/stderr no Windows, onde o encoding padrão
# do console (cp1252) corromperia texto com acentos e caracteres especiais.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
if sys.stdin.encoding and sys.stdin.encoding.lower() != "utf-8":
    import io
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")

from synesis_coder.cli import main

if __name__ == "__main__":
    main()
