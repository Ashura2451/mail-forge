# Review, feat/mailforge-html-template-interoperability-034, 2026-10-31

**Reviewed revision**: `b5bd219d4e232ef4687443fe2bddaa13a7b8a713` (HEAD; requested feature tip)
**Base**: `cb851ca6beb0a3e35fe9f38cef602504ef6bda22` (main)
**PR**: https://github.com/Ashura2451/mail-forge/pull/22
**Reviewed by**: distinct reviewer execution context/provider; implementation author identity is not available in this context
**Scope**: 21 changed files, cumulative diff `git diff cb851ca6beb0a3e35fe9f38cef602504ef6bda22 b5bd219d4e232ef4687443fe2bddaa13a7b8a713`
**Verdict**: BLOCKED

## Summary

The change adds the intended custom HTML document path, bounded ZIP asset handling, isolated preview, immutable revisions, and proof-bound rendering structure. The exact reviewed revision cannot qualify because it does not parse under Python 3, and the custom HTML transformer has a concrete malformed-attribute bypass that can retain an event handler in prepared HTML. The frontend refactor also removes existing structured Message Studio editing controls, contrary to the 034 contract.

## Blockers

### 🔴 Custom HTML import module is syntactically invalid, `src/mailforge/application/message_studio.py:654`

**Problem**: `except MessageStudioError, CustomHTMLImportError:` is Python 2 syntax and raises `SyntaxError: invalid syntax` under Python 3. The repository requires Python 3 and the changed module is imported by the new 034 tests and the web application.

**Why it matters**: The application cannot import `MessageStudioService`; custom HTML import, Message Studio startup, and the relevant test module cannot execute at this revision. This is a complete runtime/build gate failure, not an untested edge case.

**Reproducible evidence**: Independently executed `PYTHONPYCACHEPREFIX=/private/tmp/mailforge-pycache python3 -m py_compile src/mailforge/application/custom_html.py src/mailforge/application/message_studio.py`, which returned exit 1 with `SyntaxError: invalid syntax` at line 654. The same failure prevents importing `tests/test_custom_html_interoperability_034.py`.

**Suggested fix**: Use valid Python 3 exception tuple syntax, then rerun the focused suite and all dependent application checks from the exact final revision.

### 🔴 Event-handler bypass in the prepared HTML sanitizer, `src/mailforge/application/custom_html.py:69`

**Problem**: `_TAG` terminates a tag at the first `>` without respecting quoted attribute values. The later `replace_tag` logic is therefore not guaranteed to see and remove all attributes. A quoted attribute containing `>` can split the tag before the `on*` attribute is parsed.

**Why it matters**: The importer promises that event handlers and active content are rejected or removed before preview/preparation. This parser boundary can leave executable event-handler markup in `prepared_source`, which is then used by the MIME path. It defeats a central HTML safety invariant and should block acceptance until the parser is made quote-aware or replaced with a standards-aware bounded parser and regression-tested.

**Reproducible evidence**: Independently executed:

```text
analyze_html('<p title="x > y" onclick="alert(1)">Hi</p>', {})
```

returned `prepared_source` containing `<p title="&quot;x"> y" onclick="alert(1)">Hi</p>` and no `event_handler` diagnostic. The source is accepted by the current signature check. This is separate from the Python syntax failure and remains a security defect after that syntax is corrected.

**Suggested fix**: Parse tags/attributes with a quote-aware HTML tokenizer (or reject malformed/ambiguous tags) and assert that every prepared output contains no event-handler attributes, including values containing `>` or `<` inside quotes. Keep the original source only in the immutable source field; never pass unparsed source into preview or MIME preparation.

## Major

### 🟠 Structured Message Studio controls regressed, `web/src/App.tsx:617`

**Problem**: The new `renderStructured` replaces the prior structured editor surface but removes the existing text bold/italic/underline/alignment controls and the conditional-block audience-segment editor. It still offers a Conditional block in the add menu, but the resulting block has no UI to configure its condition or branches.

**Why it matters**: Requirement 1 says structured composition remains unchanged and is the default mode. Existing operators can no longer edit previously supported structured content through the operator workflow, and newly added conditional blocks can become invalid/unusable. This is a direct structured Message Studio regression, not a preference about UI organization.

**Evidence**: Cumulative diff inspection shows the old per-block controls and `block.type === 'conditional'` editor were present in the removed inline editor, while the new `renderStructured` contains only text, heading, list, button, and image editors. The backend still permits and renders those structured fields, so the loss is in the operator workflow.

**Suggested fix**: Preserve the prior structured editor controls and conditional configuration when extracting `renderStructured`; add custom HTML UI alongside the existing structured behavior and add a browser regression covering a pre-existing formatted/conditional document.

## Criterion-level 034 acceptance matrix

| Criterion | Status | Method / environment / provenance |
|---|---|---|
| Structured composition remains unchanged and is default | **FAIL** | Source/diff inspection of `web/src/App.tsx`; structured controls are removed. Independent reviewer evidence. |
| Original HTML, hash, manifest, diagnostics, and prepared representation persist in normal immutable revisions | **NOT VERIFIED** | Inspected `message_studio.py` and focused tests; Python syntax error prevents runtime execution. Executor evidence claims this behavior, but it is not independently confirmed here. |
| HTML/ZIP signature, path, size, count, depth, compression, duplicate, symlink, active-content, and reference checks | **FAIL** | Source inspection plus independent `analyze_html` reproducer; event-handler bypass exists. ZIP cases were inspected but not independently run because the service module does not parse. |
| No remote resource fetch; remote references diagnosed and removed from safe representations | **NOT VERIFIED** | Inspected sanitizer logic and tests; no browser/network runtime was independently exercised. |
| CSS/source fidelity, media-query/conditional retention, and deterministic simple inlining | **NOT VERIFIED** | Source inspection only; no independent render/client comparison. |
| Merge rendering, escaping, generated/editable plain text | **NOT VERIFIED** | Relevant code/tests inspected; Python import failure prevented execution. |
| Opaque no-origin resource-free preview | **NOT VERIFIED** | Inspected `sandbox=""` and API path; no browser journey independently executed. |
| Existing publish, campaign snapshot, renderer, MIME, eligibility, grant, reservation, idempotency, ambiguity, and proof paths | **NOT VERIFIED** | Inspected routing and proof preparation; syntax failure prevented end-to-end execution. |
| Bounded export of preserved source and owned assets without local paths | **NOT VERIFIED** | Inspected export implementation/tests; no independent runtime execution. |
| Browser persistence/operator workflows and structured regressions | **FAIL** | Source inspection identifies structured control regression; browser execution was not possible after syntax failure. |
| Packaged-runtime evidence and limits | **NOT VERIFIED** | Executor evidence was assessed, but no packaged browser or packaged Python SMTP probe was independently run. |

## SOURCE INSPECTED

- `.orchestra/EXECUTOR.md`, `.orchestra/HANDOFF.md`, `AGENTS.md`, `web/AGENTS.md`, and `DESIGN.md`.
- `docs/specs/0023-html-template-interoperability.md` and the 034 observations/closure evidence in `.aios/repo_truth.md`.
- The cumulative diff and changed implementation/test surfaces, especially `src/mailforge/application/custom_html.py`, `src/mailforge/application/message_studio.py`, `src/mailforge/interfaces/web.py`, `src/mailforge/application/outbound.py`, `src/mailforge/application/outbound_proof.py`, `web/src/App.tsx`, `web/src/api/client.ts`, `tests/test_custom_html_interoperability_034.py`, and the relevant additions in `tests/e2e/test_browser_acceptance.py`.
- Existing review rubric at `.agents/skills/check/review-guide.md` and review-mode instructions at `.agents/skills/check/modes/review.md`.

The requested `/Users/blac_goody/Desktop/pole-app/.agents/skills/check/review-agent-prompt.md` and `review-guide.md` paths were not readable in this execution sandbox; the repository-local copies above were used instead. This limitation is recorded rather than treated as evidence of a passing external rubric.

## TEST INDEPENDENTLY EXECUTED

- `PYTHONPYCACHEPREFIX=/private/tmp/mailforge-pycache python3 -m py_compile src/mailforge/application/custom_html.py src/mailforge/application/message_studio.py` — **failed** at `message_studio.py:654` with Python 3 `SyntaxError`.
- A direct, read-only `analyze_html` invocation against the quoted-`>` event-handler input — **returned unsafe prepared output without an event-handler diagnostic**, as described in the blocker above.
- No Python suite, frontend build/lint, browser journey, packaged runtime, SMTP fixture, or external-client rendering was independently executed after the syntax failure.

## EXECUTOR EVIDENCE ASSESSED

The 034 closure evidence in `.aios/repo_truth.md` reports 305 Python/browser tests, locked Ruff, frontend build/lint, exact loopback MIME capture, and a packaged embedded-runtime browser journey. It also correctly limits those claims to local synthetic evidence and says external-client rendering, real-provider qualification, signing/notarization, and cross-model approval remain unverified. Those claims are provenance-bearing repository evidence, not independently executed evidence in this review; the current source-level syntax error makes the reported final state inconsistent with the reviewed revision and requires revalidation after correction.

The packaged evidence is not interpreted as one uninterrupted desktop send workflow: the local acceptance contract can use separate packaged-browser and packaged Python SMTP probes, but neither was independently exercised here. Real SMTP, legal-policy approval, signing, and Windows packaging are not required for this review.

## NOT VERIFIED

External-client rendering, real-provider qualification, legal-policy approval, signing/notarization, Windows packaging, and any uninterrupted desktop browser-to-SMTP send workflow were not required and were not claimed. Independent browser/packaged qualification, full regression execution, MIME proof execution, persistence readback, and remote-fetch observation remain unverified because the reviewed revision fails Python parsing.

## Strengths

- The design keeps custom HTML inside the existing Message Studio/revision/composition/proof authorities rather than introducing a parallel send engine.
- The source shows deliberate bounded ZIP checks, local-only asset resolution, source-hash persistence, sandboxed preview intent, and explicit separation of original versus prepared HTML.
- The closure evidence explicitly records local-only limits and avoids claiming real-provider or external-client qualification.

## Test coverage

The changed test inventory is substantial and targets import/export, active content, remote resources, revisions, proof binding, API upload, MIME capture, browser persistence, scrolling, and packaged runtime. However, the exact reviewed revision cannot run the focused Python module because of the syntax error, and the existing tests do not cover the quote-aware parser bypass demonstrated above or the structured-editor control regression. Re-run all claimed closure checks after fixing both blockers.

**Scope**: no matching `docs/scope/` row was found for `MAILFORGE-034-DISTINCT-MODEL-REVIEW-AND-INTEGRATION`; no scope checkbox was ticked.
