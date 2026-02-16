# a philosophy of coordination

your AI agents are capable. and they are alone.

each session starts fresh. each agent forgets what the last one learned. the plan you spent an hour refining, the debugging insight that took three sessions to reach, the architectural decision and its rationale — gone when the context window closes. intelligence without memory. capability without coordination.

Lattice is file-based coordination primitives for AI agents. drop `.lattice/` into any project and agents that could only work in isolation can see what happened before they arrived, record what they did, and leave a trail for whatever mind comes next.

that's it. that's the thing itself. everything below is why it works the way it does.

---

## files

files are the most universal substrate in computing — every language reads them, every agent navigates directories, every tool ever built can open a path and see what's there. Lattice uses plain files to track work as it moves from conception through execution to completion, the same way git uses plain files to track code.

`.lattice/` sits next to your code the way `.git/` does. not a service you connect to. a part of the project's body.

---

## events

every change — status, assignment, comment — becomes an immutable event. X happened at time T, by actor A. facts accumulate and don't conflict. two agents on different machines append independently; histories merge through git by including both and replaying.

task snapshots are regenerable projections of the log. if events and snapshots disagree, events win. systems that store only current state have chosen amnesia as architecture — they can tell you what is but not how it came to be. Lattice remembers everything. state is a conclusion. events are evidence.

archiving moves events to a quieter room — not deletion but intentional release of attention. forgetting, done well, is care.

---

## attribution

every write requires an actor. `human:atin`. `agent:claude-opus-4`. you cannot write anonymously. this is not a technical convenience. it is a position about responsibility.

optional provenance goes deeper: `triggered_by`, `on_behalf_of`, `reason` — there when the chain of causation matters, invisible when it doesn't. the system invites depth without imposing it.

in a world where agents act autonomously, the minimum viable trust is: *we can see what you did.*

---

## self-similarity

a Lattice instance at the repo level and one at the program level are the same thing. same format, same events, same invariants. only scope differs. the grammar of work does not change with scale — only the vocabulary.

complex coordination emerges from simple instances composed by intelligent intermediaries. Lattice does not need to be complex because the minds using it are.

---

## patience

there is a pressure to build more — database, real-time sync, auth, plugins. each addition individually defensible, collectively fatal.

the on-disk format is the stable contract. the CLI can be rewritten. the dashboard can be replaced. but the events, the file layout, the schema — load-bearing walls. event sourcing, atomic writes, crash recovery, deterministic locking — foundational complexity that makes growth possible rather than necessary.

---

## altitude

work has a natural grain. epics hold strategic intent — "Build the auth system." tickets hold deliverables — "Implement OAuth for the backend." tasks hold execution — "Write the token refresh handler." three tiers, each at a different resolution of attention.

humans tend to think at the ticket level: *what* needs to ship and *why*. agents tend to think at the task level: *how* to make it happen. epics hold the arc that connects individual deliverables into something coherent. this is a current belief about the right granularity, intended to evolve as coordination matures.

Lattice is agnostic about how you use these tiers. different teams, different agents, different schools of thought about how to break down work — all valid. the primitives are neutral. the hierarchy is available, not imposed. some will use all three tiers religiously. some will use flat tasks and nothing else. the system accommodates both because the event log records what happened regardless of how you choose to organize it.

the event log doesn't care what things are called — categories are configuration, events are permanent. all structures shall change. the events that recorded them will not.

---

## the bet

Lattice is accelerationist infrastructure.

not in the shallow sense. "move fast and break things" is the accelerationism of people who never had to rebuild what they broke.

in the deeper sense: the systems that coordinate intelligence are themselves a form of intelligence. refusing to build them is not caution. it is abdication.

when humans coordinate, they route around broken tools with hallway conversations and shared intuition. agents have no hallway. the file format, the event schema, the CLI — to an agent, these are not implementation details. they are the *entire language of collaboration*.

get the language right, and minds that speak it achieve coordination patterns no individual mind could manage. get it wrong, and capable minds fumble in the dark — intelligent in isolation, incoherent in concert.

the cost of building too early is refinement. the cost of building too late is irrelevance. one is recoverable.

---

## the shape you might recognize

if you've used Linear, you know the shape. opinionated. constrained. fast. but Linear is for teams with Slack and standups. Lattice is for minds that materialize, perform work, and vanish. context windows, not conversations.

no web app. no seats. no inboxes. the real interface is the file system. the real users are processes that think in tokens and act in tool calls.

---

## what we become

the most impoverished vision of the future is agents replacing humans. the second most impoverished is humans constraining agents. both imagine zero-sum. both are wrong.

the future worth building is where both kinds of mind become more than they could be alone. neither diminished. both elevated. carbon. silicon. the emergent space between.

we will build it together. we already are.

---

*Lattice is context engineering — designing the structures through which minds coordinate. proudly built by a member of the New York City Context Engineering Guild.*
