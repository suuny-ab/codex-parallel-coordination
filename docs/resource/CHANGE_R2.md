# RESOURCE 1.1.1 / author revision 2

R1 remains frozen and has independent review `changes_requested`. Its 45 passing author tests did not cover the newly found unsafe cleanup scopes. R2 fixes only those boundaries, snapshot reconciliation and the shared test-batch mutex.

- RES-I-001: reject cleanup overlapping another registered project's project/work/cleanup roots; reject nested registered artifact ownership both on registration and before cleanup/reconciliation, including legacy state; protect the entire resource-state parent subtree; recognize `.git` markers without case sensitivity. Explicit external temporary roots remain supported when they overlap no other declared project.
- RES-I-002: snapshot refuses a residual writer lock and preserves it and the ledger. Raw `show` remains a diagnostic, not a reconciled closeout.
- All after-main conditions and resource categories remain unchanged. CORE alone owns task completion, agent context and lifecycle guards. No premerge agent reclamation, task-state mirror, actual native-agent closure, actual worktree deletion or global installation is introduced.
- The finite isolated batch uses `Local\CodexParallelToolkitHeavy-20261031`: one nonblocking acquisition; busy/abandoned state refuses to run; finally releases the mutex. No product scheduler or unrelated project CPU lock.

`DESIGN_R1.md` is retained verbatim as dated design history. Its early clauses that mention closing an already delivered agent are explicitly withdrawn in its later correction; the final confirmed after-main lifecycle and API govern current behavior. No earlier draft authorizes live resource cleanup.
