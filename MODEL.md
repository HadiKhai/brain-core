# Core model

The framework's abstract core. It names no person, project, category, agent product or app.
An instance binds it in its own `instance.md`. If a rule here needs a specific name to make
sense, it belongs in the instance, not here.

## Primitives

| Primitive | Meaning |
| --- | --- |
| **Principal** | Anyone who can own or be granted something: a person, a group of people, an organization. |
| **Operator** | The principal the framework works for. Sits at the root. |
| **Node** | A unit of work with its own state (files) and, optionally, its own instructions. Nodes form one tree of any depth. |
| **Owners(n)** | The non-empty set of principals that control node *n*. Unless set, a node has its parent's owners. |
| **Boundary** | Exists between a node and its parent when their owner sets differ. A node with a boundary above it is **sealed**. |
| **Label** | A `key: value` attached to a node, in any dimension and any number (area, client, stage, priority…). |
| **Role** | What a thread is for: **coordinator** (responsible for a node), **worker** (one job), **specialist** (a reusable role definition with its own standards and lessons). |
| **Runtime** | An agent engine that runs roles. Any number of runtimes may be in use. |
| **Host** | The system that runs threads and lets them reach each other. |
| **Thread** | One conversation: a runtime running a role, inside a node. |
| **Brief** | A message handed to a thread. It lives only in the thread. |
| **Item** | Reusable knowledge: a method, style, asset, script, lesson or fact. |
| **Provenance(i)** | Where item *i* came from: source node, exact reference, time, and the source's owners at that time. |
| **Scope(i)** | The set of principals whose nodes may use item *i*, or `*` for anyone. A node may use *i* only if every one of its owners is in Scope(i). |
| **Transition** | A change to the tree: create, relabel, move, transfer, split, merge, pause, archive, revoke. |

## Axioms

### Structure

1. **One tree, any depth.** No depth has special meaning. The root is a node like any other.
2. **Rules belong to nodes; labels carry none.** Labels organize and filter, nothing else. If
   something must carry rules, agents or permissions, it is a node.
3. **Boundaries are derived, never declared.** They follow from owner sets, so changing
   owners is the only way to create or remove one.

### Visibility

4. **Sight goes down.** A node may read its descendants, wherever its owners have access.
5. **Inheritance goes down and stops at a boundary.** A child inherits its ancestors'
   rules up to the nearest boundary above it, and nothing beyond.
6. **No upward traces.** Nothing inside a sealed node names, links or depends on anything
   above its boundary. It works the same for anyone who has only that node.
7. **Nearest rules win.** Work inside a node follows that node's own rules, agents and
   specialists first. Anything brought from above adds to them; it never overrides them.
8. **Layout follows sight.** Where things sit physically (folders, repos, mounts) must
   never make a runtime load rules across a boundary.

### Flow

9. **Context crosses boundaries only in briefs.** Knowledge from above reaches a node
   through the briefs of its threads, never by being written into its state.
10. **Learning up is a citation.** An item taken from a node records its provenance. Its
    scope starts as the source's owners at that time.
11. **Only the named owners can widen a scope.** Widening an item's scope needs the consent
    of every principal currently in it.
12. **Teaching down is a native copy.** Giving a node an item means writing it in that
    node's own conventions, through that node's own change process. Across a boundary, the
    receiving node's owners must consent. After that, the node owns its copy.
13. **Relations live at the nearest common ancestor.** Anything that concerns two or more
    nodes is kept at the lowest node that can see all of them.

### Roles and state

14. **Coordinators are recursive and optional.** Any node may have one. If it does, work
    for that node goes through it.
15. **One writer per file.** Everyone else asks the writer.
16. **State lives in files; threads are disposable.** Any thread can be replaced from the
    files of the node it served.
17. **Time is absolute.** Every recorded fact carries a full timestamp.

### Change

18. **Relabeling changes only labels.**
19. **Transitions preserve provenance.** An item's source, time and scope are never
    rewritten by a transition. Only axiom 11 changes scope.
20. **Boundaries are recomputed after every transition** (procedures below):
    - a new boundary means the sealing procedure runs;
    - a removed boundary means the unsealing procedure runs.
21. **Revocation quarantines.** When the operator loses its relationship with a principal,
    every item that only that principal's nodes may use is quarantined. It is kept on record
    and used nowhere.

### Portability

22. **Runtime neutrality.** Everything a role needs exists in a runtime-neutral form, with a
    thin adapter per runtime. Every axiom is tested on every runtime in use.
23. **Host neutrality.** The core uses only the host contract below. A host binding maps
    it to a real system.

### Safety

24. **Consent for what can't be taken back.** Actions that leave the instance (publish,
    push, send, deploy) or can't be undone need the explicit consent of the operator and of
    the owners of every affected node.

## Procedures

**Sealing** (a boundary appears above node *n*):

1. Split *n*'s state by owner. What belongs to *n*'s new owner set moves to a home they
   control. What belongs only to principals above the boundary stays above it.
2. Materialize inheritance. Copy into *n*, natively, every rule, role or item it still needs
   from above (axioms 5 and 12).
3. Remove every upward trace (axiom 6), and verify by search.
4. Ask the principals in scope whether the scopes of earlier items change (axiom 11).
   Until they decide, nothing changes (axiom 19).
5. *n*'s coordinator, if any, stays on the parent's side and bridges the boundary.

**Unsealing** (a boundary disappears): absorb *n*'s state into the parent's world.
Inheritance resumes. Items keep their scopes (axiom 19).

**Move** (*n* gets a new parent): it is a transfer if the owner sets now differ, otherwise
just a re-filing. Either way, recompute boundaries (axiom 20).

**Split / merge**: each resulting node gets an owner set, then boundaries are recomputed. Items
keep their provenance, pointing at the original node.

**Pause / archive**: the coordinator writes a final status, and threads stop. State stays
readable.

**Revoke** (the operator's relationship with a principal ends): the axiom 21 quarantine,
plus archiving every node that principal alone now owns.

## Runtime contract

A runtime can serve this framework if it can:

1. Load a node's instructions from where it runs, following inheritance up to the nearest
   boundary.
2. Accept a brief.
3. Read and write the node's files.
4. Call the host contract.

Each runtime's adapter maps the neutral forms (instructions, roles, skills) to that
runtime's own file names and formats.

## Host contract

| Operation | Meaning |
| --- | --- |
| `launch(node, role, runtime, brief)` | Start a thread in a node, optionally in an isolated copy of its state. |
| `send(thread, brief)` | Message a running thread. |
| `wait(thread)` | Block until the thread finishes its turn. |
| `read(thread)` | Read a thread's history. |
| `list(node)` | List a node's threads and their last activity. |
| `delegate(runtime, brief)` | One-shot task with no node memory. |
| `schedule(when, brief)` | Recurring or triggered work. |
