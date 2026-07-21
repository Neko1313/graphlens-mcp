"""Navigation workflows as prompts — each drives the search / relations / info
tools through a fixed method so the agent doesn't have to improvise one.
"""

__all__ = ["deadcode", "find", "impact", "map_area", "trace", "xflow"]


def impact(symbol: str, file: str = "") -> str:
    """Blast radius of changing a symbol (rename / signature / removal)."""
    where = f" (in {file})" if file else ""
    return (
        f"Assess the impact of changing `{symbol}`{where} with graphlens.\n\n"
        "1. Resolve it: info(`{symbol}`) — if it returns candidates, pick the "
        "intended one by id (or pass file=).\n"
        "2. relations(symbol, depth=2). Read `callers_total` (and "
        "`references_total`) to size the blast radius before reading "
        "individual callers.\n"
        "3. Group the callers by module to see the affected areas.\n"
        "4. Raise depth only where the frontier is small and live; read a "
        "caller's source with info(id) only where it matters.\n"
        "5. For full file enumeration, escalate to search(exhaustive=True).\n"
        "6. State the completeness caveat: dynamic dispatch, DI, reflection, "
        "cross-language, and un-indexed code can hide callers; report "
        "`callers_unresolved` if non-zero. Covers rename, signature/type "
        "change, API surface, test impact, and removability."
    )


def find(query: str) -> str:
    """Locate the code that does something, then narrow to the symbol."""
    return (
        f"Find the code responsible for: {query}\n\n"
        "Locate-then-narrow:\n"
        "1. search(query) — semantic + name + content across the project.\n"
        "2. On the top hit, follow its resource link or call info(id) to read "
        "the source.\n"
        "3. If it's a jumping-off point, call relations(id) to see callers/"
        "callees, and iterate until you've pinned the code."
    )


def trace(start: str, end: str = "") -> str:
    """Trace the call path from one symbol, optionally toward another."""
    target = f" to `{end}`" if end else ""
    tail = (
        f"3. Stop when you reach `{end}`; report the chain of calls.\n"
        if end
        else "3. Keep expanding callees to map the reachable call tree.\n"
    )
    return (
        f"Trace the call path from `{start}`{target}, end to end.\n\n"
        "1. Resolve `{start}` with info; take its id.\n"
        "2. relations(id, kinds=calls); follow each callee with relations "
        "again, depth-first, crossing cross-language edges at boundaries.\n"
        + tail
        + "Note any `callees_unresolved` — calls graphlens couldn't follow."
    )


def map_area(area: str) -> str:
    """Map a subsystem for onboarding: files, key symbols, links."""
    return (
        f"Map the `{area}` subsystem for onboarding.\n\n"
        "1. search(area) for its entry points, scoping with path_glob if you "
        "know the directory.\n"
        "2. For each relevant file, info(path) for its outline.\n"
        "3. Traverse relations on the load-bearing symbols to see how the "
        "pieces connect.\n"
        "4. Read only the load-bearing nodes' source; summarise the structure "
        "rather than dumping every file."
    )


def xflow(symbol: str) -> str:
    """Cross-language / service consumers of a handler, route, or topic."""
    return (
        f"Follow the cross-language / cross-service flow around `{symbol}`.\n"
        "\n1. Land on the handler: resolve `{symbol}` with info and read it.\n"
        "2. relations(symbol) with kinds covering references and calls; find "
        "the boundary edges (route↔client, pub↔sub) that cross a language or "
        "service boundary.\n"
        "3. Follow those edges to the consumer/producer on the other side "
        "and continue until the flow is complete."
    )


def deadcode(symbol: str) -> str:
    """Removability check: is anything referencing this symbol?"""
    return (
        f"Check whether `{symbol}` is removable (dead code).\n\n"
        "1. Resolve it with info; take its id.\n"
        "2. relations(id) and check every group: `callers_total`, "
        "`references_total`, and `implementors_total` all == 0 suggests it "
        "may be removable.\n"
        "3. Caveat clearly: zero edges is NOT proof it's dead — dynamic "
        "dispatch, reflection, DI, cross-language use, entry points, and "
        "un-indexed callers can all reference it without a graph edge."
    )
