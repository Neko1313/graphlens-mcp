"""Navigation workflows as prompts.

Each turns a request into a fixed method over the search / relations / info
tools, so the agent doesn't have to improvise one. **Every argument is
optional**: name a symbol, describe what you mean in plain words, or say
nothing and let the agent take the subject from the conversation — ``search``
resolves a description to a symbol, so callers never need a node id up front.
"""

__all__ = ["deadcode", "find", "impact", "map_area", "trace", "xflow"]


def _subject(value: str, noun: str) -> str:
    return f"`{value}`" if value else f"the {noun} in question"


# The shared "resolve the subject" preamble: handles a name, a plain-language
# description, or nothing at all.
_PIN = (
    "Pin it down first: if it's a name or node id, `info` it; if it's a "
    "plain-language description, `search` first and `info` the best hit; if "
    "nothing was named, use what the user is discussing.\n"
)


def impact(target: str = "") -> str:
    """Blast radius of changing something (rename / signature / removal)."""
    return (
        "Assess the blast radius of changing "
        f"{_subject(target, 'symbol')} with graphlens.\n\n"
        + _PIN
        + "1. relations(id, depth=2); read `callers_total` / "
        "`references_total` to size the impact before reading individual "
        "callers.\n"
        "2. Group callers by module to see the affected areas; raise depth "
        "only where the frontier is small and live.\n"
        "3. Read a caller's source with info(id) only where it matters; for "
        "full file enumeration escalate to search(exhaustive=True).\n"
        "4. State the caveat: dynamic dispatch, DI, reflection, "
        "cross-language, and un-indexed code can hide callers — report "
        "`callers_unresolved` if non-zero."
    )


def find(query: str = "") -> str:
    """Locate the code responsible for something, then narrow to the symbol."""
    subject = f"`{query}`" if query else "what the user is asking about"
    return (
        f"Find the code responsible for {subject}.\n\n"
        "1. search(query) — semantic + name + content across the project. A "
        "plain-language description is fine; it need not be a symbol name.\n"
        "2. Follow the top hit's resource link or call info(id) to read its "
        "source.\n"
        "3. If it's a jumping-off point, relations(id) for callers/callees, "
        "and iterate until the code is pinned."
    )


def trace(start: str = "", end: str = "") -> str:
    """Trace the call path from one point, optionally toward another."""
    toward = f" to `{end}`" if end else ""
    tail = (
        f"2. Stop when you reach `{end}`; report the chain of calls.\n"
        if end
        else "2. Keep expanding callees to map the reachable call tree.\n"
    )
    return (
        f"Trace the call path from {_subject(start, 'starting symbol')}"
        f"{toward}, end to end.\n\n"
        + _PIN
        + "1. relations(id, kinds=calls); follow each callee with relations "
        "again, depth-first, crossing cross-language edges at boundaries.\n"
        + tail
        + "Note any `callees_unresolved` — calls graphlens couldn't follow."
    )


def map_area(area: str = "") -> str:
    """Map a subsystem for onboarding: files, key symbols, links."""
    return (
        f"Map {_subject(area, 'area or subsystem')} for onboarding.\n\n"
        "1. search(area) for its entry points, scoping with path_glob if you "
        "know the directory — a description works, it needn't be a path.\n"
        "2. info(path) for each relevant file's outline.\n"
        "3. Traverse relations on the load-bearing symbols to see how the "
        "pieces connect.\n"
        "4. Read only the load-bearing nodes' source; summarise the structure "
        "rather than dumping every file."
    )


def xflow(target: str = "") -> str:
    """Cross-language / cross-service flow around a handler, route or topic."""
    return (
        "Follow the cross-language / cross-service flow around "
        f"{_subject(target, 'handler, route, or topic')}.\n\n"
        + _PIN
        + "1. relations(id) covering references and calls; find the boundary "
        "edges (route↔client, pub↔sub) that cross a language or service "
        "boundary.\n"
        "2. Follow those edges to the consumer/producer on the other side "
        "and continue until the flow is complete."
    )


def deadcode(target: str = "") -> str:
    """Removability check: is anything referencing this symbol?"""
    return (
        f"Check whether {_subject(target, 'symbol')} is removable (dead "
        "code).\n\n"
        + _PIN
        + "1. relations(id) and check every group: `callers_total`, "
        "`references_total`, and `implementors_total` all == 0 suggests it "
        "may be removable.\n"
        "2. Caveat clearly: zero edges is NOT proof — dynamic dispatch, "
        "reflection, DI, cross-language use, entry points, and un-indexed "
        "callers can all reference it without a graph edge."
    )
