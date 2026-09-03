# -*- coding: utf-8 -*-
"""T-SQL procedure parser for step-by-step debugging.

Extracts everything the engine needs from a CREATE [OR ALTER] PROCEDURE —
without touching the file: name, parameters (type, default, OUTPUT), body,
and the slicing of the body into control-flow-aware steps:

- statements terminated by ';' at nesting level 0 (parens and BEGIN/CASE/END
  are tracked);
- IF/WHILE become block steps with their branch structure preserved
  (condition + body per branch), which lets step_into() evaluate the
  condition server-side and expand only the chosen branch;
- each BEGIN TRY is unwrapped and gets its own CATCH block for emulation;
- every DECLARE (at any depth) is discoverable, because in T-SQL variable
  scope is the BATCH, not the block;
- RETURN becomes its own step so the debugger can stop where the real
  execution would stop.
"""

import re

from .scanner import is_punct, is_word, scan

STMT_START = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "EXECUTE", "SET",
    "RAISERROR", "PRINT", "THROW", "RETURN", "BREAK", "CONTINUE", "DECLARE",
    "BEGIN", "IF", "WHILE", "GOTO", "WAITFOR", "TRUNCATE", "DROP", "CREATE",
    "ALTER", "WITH", "COMMIT", "ROLLBACK", "SAVE",
}

# BEGIN followed by one of these does NOT open a block (BEGIN TRAN has no
# matching END of its own)
_NON_BLOCK_BEGIN = {"TRAN", "TRANSACTION", "DISTRIBUTED", "DIALOG", "CONVERSATION"}


def is_block_begin(tokens, i, i1):
    """True when tokens[i] is a block-opening BEGIN (not BEGIN TRAN/TRANSACTION)."""
    if not is_word(tokens[i], "BEGIN"):
        return False
    nxt = tokens[i + 1] if i + 1 < i1 else None
    return not (nxt is not None and nxt["k"] == "w" and nxt["u"] in _NON_BLOCK_BEGIN)


def find_procedure(tokens):
    """Return the index of the token right after PROCEDURE, or None if absent."""
    for i, t in enumerate(tokens):
        if is_word(t, "CREATE"):
            j = i + 1
            if j + 1 < len(tokens) and is_word(tokens[j], "OR") and is_word(tokens[j + 1], "ALTER"):
                j += 2
            if j < len(tokens) and tokens[j]["k"] == "w" and tokens[j]["u"] in ("PROCEDURE", "PROC"):
                return j + 1
    return None


def parse_params(sql, tokens, i):
    """Starting at the procedure name, consume name and parameters up to AS.

    Returns (qualified_name, param_list, index_after_AS). Each param:
    {"name", "type", "default", "output"}.
    """
    name_parts = []
    while i < len(tokens):
        t = tokens[i]
        if t["k"] in ("w", "brk") and not (t["k"] == "w" and t["u"].startswith("@")):
            if is_word(t, "AS"):
                break
            name_parts.append(sql[t["s"]:t["e"]])
            i += 1
        elif is_punct(t, "."):
            i += 1
        else:
            break

    wrapped = i < len(tokens) and is_punct(tokens[i], "(")
    if wrapped:
        i += 1

    param_defs = []
    parens = 0
    while i < len(tokens):
        t = tokens[i]
        if parens == 0 and is_word(t, "AS"):
            i += 1
            break
        if wrapped and parens == 0 and is_punct(t, ")"):
            i += 1
            continue
        if t["k"] == "w" and t["u"].startswith("@"):
            param = {"name": sql[t["s"]:t["e"]], "type": None, "default": None, "output": False}
            i += 1
            # parameter type: runs until '=', ',', OUTPUT or AS at level 0
            t_start = t_end = None
            while i < len(tokens):
                t = tokens[i]
                if is_punct(t, "("):
                    parens += 1
                elif is_punct(t, ")"):
                    if wrapped and parens == 0:
                        break
                    parens -= 1
                if parens == 0 and (is_punct(t, ",") or is_punct(t, "=")
                                    or (t["k"] == "w" and t["u"] in ("OUTPUT", "OUT", "AS", "READONLY"))):
                    break
                if t_start is None:
                    t_start = t["s"]
                t_end = t["e"]
                i += 1
            param["type"] = sql[t_start:t_end] if t_start is not None else "SQL_VARIANT"
            # parameter default
            if i < len(tokens) and is_punct(tokens[i], "="):
                i += 1
                d_start = d_end = None
                while i < len(tokens):
                    t = tokens[i]
                    if is_punct(t, "("):
                        parens += 1
                    elif is_punct(t, ")"):
                        if wrapped and parens == 0:
                            break
                        parens -= 1
                    if parens == 0 and (is_punct(t, ",")
                                        or (t["k"] == "w" and t["u"] in ("OUTPUT", "OUT", "AS"))):
                        break
                    if d_start is None:
                        d_start = t["s"]
                    d_end = t["e"]
                    i += 1
                param["default"] = sql[d_start:d_end] if d_start is not None else None
            # OUTPUT / comma
            while i < len(tokens) and tokens[i]["k"] == "w" and tokens[i]["u"] in ("OUTPUT", "OUT", "READONLY"):
                param["output"] = tokens[i]["u"] in ("OUTPUT", "OUT") or param["output"]
                i += 1
            if i < len(tokens) and is_punct(tokens[i], ","):
                i += 1
            param_defs.append(param)
        else:
            i += 1
    return ".".join(name_parts), param_defs, i


def procedure_body(tokens, i_after_as):
    """Locate the body's BEGIN...END. Returns (i0, i1) — the inner token span."""
    i = i_after_as
    while i < len(tokens) and not is_word(tokens[i], "BEGIN"):
        i += 1
    if i >= len(tokens):
        raise ValueError("Procedure body (BEGIN...END) not found.")
    depth = 1
    j = i + 1
    while j < len(tokens):
        t = tokens[j]
        if t["k"] == "w":
            if is_word(t, "CASE") or is_block_begin(tokens, j, len(tokens)):
                depth += 1
            elif t["u"] == "END":
                depth -= 1
                if depth == 0:
                    return i + 1, j
        j += 1
    raise ValueError("END of procedure body not found.")


def skip_stmt(tokens, i, i1):
    """Advance past the level-0 ';' (parens and BEGIN/CASE/END tracked)."""
    parens = block = 0
    while i < i1:
        t = tokens[i]
        if is_punct(t, "("):
            parens += 1
        elif is_punct(t, ")"):
            parens -= 1
        elif t["k"] == "w" and (is_word(t, "CASE") or is_block_begin(tokens, i, i1)):
            block += 1
        elif is_word(t, "END"):
            block -= 1
        elif is_punct(t, ";") and parens == 0 and block <= 0:
            return i + 1
        i += 1
    return i1


def skip_block(tokens, i, i1):
    """i points at BEGIN; returns the index right after the matching END."""
    depth = 0
    while i < i1:
        t = tokens[i]
        if t["k"] == "w":
            if is_word(t, "CASE") or is_block_begin(tokens, i, i1):
                depth += 1
            elif t["u"] == "END":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return i1


def consume_branch_body(tokens, i, i1):
    """Consume a branch body (BEGIN block, nested conditional, or statement).

    Returns (index_after_body, body_token_span) — the span excludes BEGIN/END.
    """
    if i < i1 and is_block_begin(tokens, i, i1):
        b0 = i + 1
        i = skip_block(tokens, i, i1)
        return i, (b0, i - 1)
    if i < i1 and tokens[i]["k"] == "w" and tokens[i]["u"] in ("IF", "WHILE"):
        b0 = i
        i, _, _ = parse_conditional(tokens, i, i1)
        return i, (b0, i)
    b0 = i
    i = skip_stmt(tokens, i, i1)
    return i, (b0, i)


def parse_conditional(tokens, i, i1):
    """i points at IF/WHILE. Consume the full statement (with the ELSE chain).

    Returns (final_index, branches, is_loop). Each branch is a dict of token
    spans: {"cond": (c0, c1) | None for ELSE, "body": (b0, b1)} — which is
    what lets step_into() evaluate the condition server-side and expand only
    the chosen branch.
    """
    is_loop = tokens[i]["u"] == "WHILE"
    i += 1
    branches = []
    while True:
        # condition: runs until BEGIN or a statement-starting keyword at level 0
        c0 = i
        parens = 0
        while i < i1:
            t = tokens[i]
            if is_punct(t, "("):
                parens += 1
            elif is_punct(t, ")"):
                parens -= 1
            elif t["k"] == "w" and parens == 0 and t["u"] in STMT_START:
                break
            i += 1
        cond_span = (c0, i)
        i, body_span = consume_branch_body(tokens, i, i1)
        if i < i1 and is_punct(tokens[i], ";"):
            i += 1
        branches.append({"cond": cond_span, "body": body_span})
        # ELSE / ELSE IF chain (only IF has ELSE)
        if not is_loop and i < i1 and is_word(tokens[i], "ELSE"):
            i += 1
            if i < i1 and is_word(tokens[i], "IF"):
                i += 1
                continue
            i, body_span = consume_branch_body(tokens, i, i1)
            if i < i1 and is_punct(tokens[i], ";"):
                i += 1
            branches.append({"cond": None, "body": body_span})
        return i, branches, is_loop


def find_try_catch_end(tokens, i, i1, suffix):
    """i points right after BEGIN TRY/CATCH; returns the END TRY/CATCH index."""
    depth = 1
    while i < i1:
        t = tokens[i]
        if t["k"] == "w":
            if is_word(t, "CASE") or is_block_begin(tokens, i, i1):
                depth += 1
            elif t["u"] == "END":
                depth -= 1
                if depth == 0:
                    if i + 1 < i1 and is_word(tokens[i + 1], suffix):
                        return i
                    raise ValueError(f"END {suffix} expected but not found.")
        i += 1
    raise ValueError(f"END {suffix} not found.")


def split_steps(sql, tokens, i0, i1, ctx, catch_id=None):
    """Slice a body span into executable steps.

    IF/WHILE blocks become single steps with their branch structure attached.
    Each BEGIN TRY is unwrapped and gets its own entry in ctx["catches"];
    inner steps carry step["catch_id"] pointing at THEIR catch block — a
    procedure with one TRY/CATCH per phase emulates the right catch in each
    phase. RETURN becomes its own step (it ends the debug).
    """
    step_list = []
    i = i0
    while i < i1:
        t = tokens[i]
        if is_punct(t, ";"):
            i += 1
            continue
        if is_word(t, "BEGIN") and i + 1 < i1 and is_word(tokens[i + 1], "TRY"):
            try_end = find_try_catch_end(tokens, i + 2, i1, "TRY")
            my_catch_id = len(ctx["catches"])
            ctx["catches"].append([])
            step_list.extend(split_steps(sql, tokens, i + 2, try_end, ctx, catch_id=my_catch_id))
            i = try_end + 2  # skip END TRY
            if i + 1 < i1 and is_word(tokens[i], "BEGIN") and is_word(tokens[i + 1], "CATCH"):
                catch_end = find_try_catch_end(tokens, i + 2, i1, "CATCH")
                ctx["catches"][my_catch_id] = split_steps(sql, tokens, i + 2, catch_end, ctx)
                i = catch_end + 2
            continue
        if t["k"] == "w" and t["u"] in ("IF", "WHILE"):
            end, branches, is_loop = parse_conditional(tokens, i, i1)
            step = new_step(sql, tokens, i, end, t["u"].lower() + "_block", catch_id)
            step["branches"] = branches
            step["is_loop"] = is_loop
            step_list.append(step)
            i = end
            continue
        if is_word(t, "DECLARE"):
            end = skip_stmt(tokens, i, i1)
            step_list.append(new_step(sql, tokens, i, end, "declare", catch_id))
            i = end
            continue
        if is_word(t, "RETURN"):
            end = skip_stmt(tokens, i, i1)
            step_list.append(new_step(sql, tokens, i, end, "return", catch_id))
            i = end
            continue
        end = skip_stmt(tokens, i, i1)
        step_list.append(new_step(sql, tokens, i, end, "stmt", catch_id))
        i = end
    return step_list


def new_step(sql, tokens, i_start, i_end, kind, catch_id=None):
    j = i_end - 1
    while j > i_start and is_punct(tokens[j], ";"):
        j -= 1
    s, e = tokens[i_start]["s"], tokens[j]["e"]
    return {
        "kind": kind,
        "s": s,
        "e": e,
        "ti": (i_start, i_end),   # token span, for step_into and DECLARE scans
        "text": sql[s:e],
        "line": sql.count("\n", 0, s) + 1,
        "catch_id": catch_id,     # index of this step's CATCH block (None = outside TRY)
        "try": catch_id is not None,
        "depth": 0,
    }


def scan_transaction_controls(sql, tokens, i0, i1):
    """Locate COMMIT/ROLLBACK/BEGIN TRAN/SAVE TRAN in the procedure body.

    Procedures that manage their own transaction defeat the debugger's
    rollback-by-default guarantee — the caller uses this to warn.
    Returns [(keyword, line)].
    """
    found = []
    i = i0
    while i < i1:
        t = tokens[i]
        if t["k"] == "w":
            nxt = tokens[i + 1] if i + 1 < i1 else None
            if t["u"] in ("COMMIT", "ROLLBACK"):
                found.append((t["u"], sql.count("\n", 0, t["s"]) + 1))
            elif (t["u"] in ("BEGIN", "SAVE") and nxt is not None
                  and nxt["k"] == "w" and nxt["u"] in ("TRAN", "TRANSACTION")):
                found.append((f"{t['u']} {nxt['u']}", sql.count("\n", 0, t["s"]) + 1))
        i += 1
    return found


def scan_declares(sql, tokens, i0, i1):
    """Find ALL DECLARE statements in a token span, at any depth.

    This is what makes variables declared inside IF/WHILE blocks visible: in
    T-SQL, variable scope is the BATCH (not the block), so they can be
    captured and re-injected in later steps just like top-level ones.
    """
    found = []
    i = i0
    while i < i1:
        if is_word(tokens[i], "DECLARE"):
            end = skip_stmt(tokens, i, i1)
            text = sql[tokens[i]["s"]:tokens[min(end, i1) - 1]["e"]]
            found.extend(extract_declares(text))
            i = end
        else:
            i += 1
    return found


def extract_declares(text):
    """Extract [(name, type, init_expr|None)] from ONE DECLARE statement."""
    tokens = scan(text)
    result = []
    i = 0
    while i < len(tokens) and not is_word(tokens[i], "DECLARE"):
        i += 1
    i += 1
    while i < len(tokens):
        t = tokens[i]
        if not (t["k"] == "w" and t["u"].startswith("@")):
            i += 1
            continue
        var_name = text[t["s"]:t["e"]]
        i += 1
        parens = 0
        t_start = t_end = None
        while i < len(tokens):
            t = tokens[i]
            if is_punct(t, "("):
                parens += 1
            elif is_punct(t, ")"):
                parens -= 1
            if parens == 0 and (is_punct(t, ",") or is_punct(t, "=") or is_punct(t, ";")):
                break
            if t_start is None:
                t_start = t["s"]
            t_end = t["e"]
            i += 1
        var_type = text[t_start:t_end] if t_start is not None else "SQL_VARIANT"
        init_expr = None
        if i < len(tokens) and is_punct(tokens[i], "="):
            i += 1
            e_start = e_end = None
            while i < len(tokens):
                t = tokens[i]
                if is_punct(t, "("):
                    parens += 1
                elif is_punct(t, ")"):
                    parens -= 1
                if parens == 0 and (is_punct(t, ",") or is_punct(t, ";")):
                    break
                if e_start is None:
                    e_start = t["s"]
                e_end = t["e"]
                i += 1
            init_expr = text[e_start:e_end] if e_start is not None else None
        result.append((var_name, var_type, init_expr))
        if i < len(tokens) and is_punct(tokens[i], ","):
            i += 1
    return result


def eval_literal(expr):
    """Try to evaluate a simple default in Python. Returns (ok, value)."""
    if expr is None:
        return False, None
    e = expr.strip()
    if e.upper() == "NULL":
        return True, None
    m = re.fullmatch(r"N?'((?:[^']|'')*)'", e, flags=re.S)
    if m:
        return True, m.group(1).replace("''", "'")
    m = re.fullmatch(r"-?\d+", e)
    if m:
        return True, int(e)
    m = re.fullmatch(r"-?\d+\.\d+", e)
    if m:
        return True, float(e)
    return False, None


def rewrite(text, error_msg, has_rowcount_env):
    """Replace @@ROWCOUNT and ERROR_MESSAGE() with '?' parameters.

    Preserves cross-batch semantics: @@ROWCOUNT gets the previous step's
    value and ERROR_MESSAGE() gets the error captured by Python.
    Returns (text, markers).
    """
    tokens = scan(text)
    swaps = []  # (start, end, marker)
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t["k"] == "w" and t["u"] == "@@ROWCOUNT" and has_rowcount_env:
            swaps.append((t["s"], t["e"], "@@ROWCOUNT"))
        elif (t["k"] == "w" and t["u"] == "ERROR_MESSAGE" and error_msg is not None
              and i + 2 < len(tokens) and is_punct(tokens[i + 1], "(") and is_punct(tokens[i + 2], ")")):
            swaps.append((t["s"], tokens[i + 2]["e"], "ERROR_MESSAGE"))
            i += 3
            continue
        i += 1
    markers = [m for (_, _, m) in swaps]
    for s, e, _ in reversed(swaps):
        text = text[:s] + "?" + text[e:]
    return text, markers
