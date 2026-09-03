# -*- coding: utf-8 -*-
"""T-SQL lexical scanner.

Tokenizes the text while skipping the contents of strings ('...'), comments
(-- and /* */, with nesting) and bracketed/quoted identifiers — the
foundation that keeps the parser and the rewrite engine from ever mistaking
a ';' or an '@@ROWCOUNT' inside a literal for real code.

Each token is a dict: {"k": kind, "s": start, "e": end, "u": upper text}
with kinds 'w' (word), 'str', 'brk' ([..] or ".."), 'num' and 'p' (punct).
"""


def scan(sql: str) -> list:
    """Tokenize T-SQL preserving positions in the original text."""
    tokens = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n":
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
        elif c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            tokens.append({"k": "str", "s": i, "e": min(j + 1, n)})
            i = min(j + 1, n)
        elif c == "[":
            j = sql.find("]", i + 1)
            while 0 <= j < n - 1 and sql[j + 1] == "]":
                j = sql.find("]", j + 2)
            end = (j + 1) if j >= 0 else n
            tokens.append({"k": "brk", "s": i, "e": end})
            i = end
        elif c == '"':
            j = sql.find('"', i + 1)
            end = (j + 1) if j >= 0 else n
            tokens.append({"k": "brk", "s": i, "e": end})
            i = end
        elif c.isalpha() or c in "@#_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "@#_$"):
                j += 1
            tokens.append({"k": "w", "s": i, "e": j, "u": sql[i:j].upper()})
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            tokens.append({"k": "num", "s": i, "e": j})
            i = j
        else:
            tokens.append({"k": "p", "s": i, "e": i + 1, "u": c})
            i += 1
    return tokens


def is_word(token: dict, word: str) -> bool:
    return token["k"] == "w" and token["u"] == word


def is_punct(token: dict, ch: str) -> bool:
    return token["k"] == "p" and token["u"] == ch
