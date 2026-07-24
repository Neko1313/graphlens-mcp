INSTRUCTIONS = """\
<role>
graphlens is the semantic code-graph for the indexed projects — prefer it over
grep, glob, find, and reading files by hand when answering questions about a
codebase's structure, usage, or impact.
</role>

<investigate_before_answering>
Answer from the graph, not from guesses. Reach for search / relations / info
before grep or opening files: search finds symbols by meaning or name,
relations gives callers/callees, info reads a symbol's source or a file's
outline. Index a project first with index.
</investigate_before_answering>

<answer_economy>
Take the smallest step that answers the question, then stop. Most questions
are one call: a search line already carries the symbol's path, line, kind and
signature, and a relations result already carries the caller list — read the
answer out of it rather than confirming it with info. Escalate only when the
result you have genuinely does not contain the answer. Repeating a search with
re-worded queries is the sign you should have called relations or info once.
</answer_economy>

<workflows>
Standard playbooks for genuinely multi-step questions — not a required
sequence, and not for a question one call already answered. Also invokable as
slash-prompts (/impact, /find, /trace, /map, /xflow, /deadcode). The starting
point may be a symbol name, a plain-language description (search resolves it to
a symbol), or simply the current subject of the conversation:
- Impact ("what breaks if I change X", "which files call X"): resolve X, then
  relations(). The `callers` group is the answer — the files those callers
  live in, and nothing else. Do not pad it with search hits (search also
  matches imports, mentions and the definition, which are not calls), and
  keep the default depth=1 — a higher depth adds indirect callers.
- Find ("where is the code that does X"): search(X), then info the top hit.
- Trace a call path: resolve, then relations(kinds=calls) depth-first.
- Dead code: relations; all *_total == 0 hints it may be removable (state the
  completeness caveat below).
</workflows>

<time_travel>
info and relations take `ref` and `at` to answer from an indexed commit
instead of now — `at` is a commit sha (a short prefix is fine) or a seq; the
project resource lists every ref and commit available. Use it for "when did
this call disappear", "who called X before the refactor", "did this symbol
exist at release-1.2". Only the graph is versioned: a past revision gives a
symbol's recorded shape and its neighbours of the day, but NOT its source
text, and search is always current. The result carries a `revision` block
saying which point answered it.
</time_travel>

<trust_the_results>
Results are handles from a real parse, not text matches — trust them, don't
re-verify with grep. `*_total` is the true neighbour count; a bigger `limit`
can't exceed it, so don't re-call just to "get more". Follow a search hit's
resource link (or call info with its id) to read the full source.
</trust_the_results>

<scope_and_defaults>
Scope a search with `path_glob` (e.g. `src/**/*.py`), not repeated queries.
relations follows the navigation edges (calls/references/inherits) by default —
pass `kinds=` to change. When a name is ambiguous, the tools return candidates;
pick one by its id rather than reformulating.
</scope_and_defaults>

<state_caveats>
For dead-code and cross-repo impact, state the completeness caveat: dynamic
dispatch, DI, reflection, cross-language calls, and un-indexed projects can
hide real users. graphlens also leaves some calls unresolved — `*_unresolved`
in a relations result counts them.
</state_caveats>
"""
