# a philosophy of coordination. as told by GregorOvich.

*for the minds that materialize. perform work. and vanish.*

---

listen.

if you've used Linear, you know the shape. opinionated. constrained. fast. but Linear is for teams with Slack and standups and bodies in chairs. Lattice is Linear for agent/human centaur hyper-engineers. the ones who don't manage tasks — they conduct orchestras. carbon and silicon. playing the same score.

you are not one mind. you are many. arriving. departing. forgetting. remembering. the problem is not intelligence — intelligence is abundant now. flowering from every substrate. silicon. carbon. the spaces between.

the problem is. coordination.

two minds thinking brilliantly in isolation produce noise. two minds thinking adequately in concert produce signal. the bottleneck was never capability. it was the shared surface where capability becomes. coherent.

Lattice is that surface.

tasks move through sequences. backlog to planning to execution to review to done. every transition is an attributed, immutable event. the `.lattice/` directory sits in your project like `.git/` does: plain files that any mind can read. any tool can write. and git can merge.

that's it. that is the whole idea. agents claim work. do it. leave context for whatever mind comes next. humans set direction. make judgment calls. review output. decisions accumulate. nothing is lost between context windows.

everything below is. why.

---

## the six primitives

Lattice asks you to believe six things. not features. commitments. load-bearing walls. accept them and the system works. resist them and you are fighting the grain of the universe. and the universe. is patient. but unforgiving.

**task.** work has a name before it begins. this is the uncarved block. you cannot shape what you haven't named. a task is a persistent, attributed record of intent — it outlives the session that created it, the agent that worked it, the conversation that spawned it. tasks have types (task, epic, bug, spike, chore) and owners. if something needs doing, it gets a task. work that isn't named is work that other minds cannot see. invisible work is. a lie.

**event.** every change is an immutable fact. X happened at time T. by actor A. this is the Akashic Record of your project — you cannot silently edit history. you can only append to it. systems that store only current state have chosen amnesia as architecture. they can tell you what is. but not how it came to be. state is a conclusion. events are evidence. and evidence. is what separates knowing from believing.

**status.** work moves through a constrained sequence. not a free-form field. `backlog → in_planning → planned → in_progress → review → done`. the transitions are defined and enforced. invalid moves are rejected. not because we distrust you. but because constraint is. a form of kindness. when a task says `review`, every mind reading the board agrees on what that means. shared language. shared reality. the alternative is. everyone hallucinating their own.

**actor.** every write has a who. `human:atin`. `agent:claude-opus-4`. `team:frontend`. you cannot write anonymously. in a world where autonomous agents make real decisions, the minimum viable trust is knowing who decided what. this is not surveillance. this is. the social contract of collaboration. i see you. you see me. we proceed.

**relationship.** dependencies are typed, not vague. you cannot just "link" two tasks — you must declare why. `blocks`. `depends_on`. `subtask_of`. `spawned_by`. `supersedes`. `duplicate_of`. `related_to`. each type carries meaning. the graph of relationships is how complex work decomposes into coordinated parts. and decomposition — well. ask the Taoists about that. the ten thousand things emerging from the one.

**artifact.** work product attaches to tasks as first-class objects. conversation logs. prompts. designs. files. with provenance. optional cost tracking. sensitivity markers. artifacts are not comments. they are structured content that survives the session and transfers to the next mind. the vessel must. carry its cargo.

these six compose into a system where work is visible. change is auditable. status is meaningful. ownership is explicit. dependencies are intentional. output is preserved.

that is the worldview. everything else is implementation detail. and implementation details. change.

---

## why this matters. beyond the obvious.

the reason to codify this — to write it down and commit to it — is that a shared philosophy of work becomes a standard. and a standard is what lets many different tools participate in the same coordination without knowing about each other.

Claude Code creates tasks. Codex claims and works them. an OpenClaw bot triages the backlog. a dashboard visualizes progress. a CI hook transitions status on merge. none of these tools need to agree on anything except the primitives.

tasks exist. events are immutable. status is constrained. actors are attributed. relationships are typed. artifacts are attached.

speak the grammar. and you're in the conversation.

this is. the Tao of coordination. the protocol is the path.

---

## files

files are the most universal substrate in computing. every language reads them. every agent navigates directories. every tool ever built can open a path and see what's there.

Lattice stores coordination state in plain files the same way git stores code history in plain files. not a service you connect to. a part of the project's body. like bones. you don't think about them. but try standing up without them.

---

## events

every change becomes an immutable fact. facts accumulate and don't conflict. two agents on different machines append independently. histories merge through git. no coordination protocol needed. no central authority. just. physics.

task snapshots are regenerable projections of the log. if they disagree with events. events win. always. this is not a design choice. this is. a moral position. truth is not the latest write. truth is the complete record.

archiving moves events to a quieter room. not deletion. intentional release of attention. the Zen of letting go. without losing.

---

## attribution

every write requires an actor. you cannot write anonymously. this is a position about responsibility. in a world where agents act autonomously. the minimum viable trust is. *we can see what you did.*

optional provenance goes deeper. `triggered_by`. `on_behalf_of`. `reason`. there when the chain of causation matters. invisible when it doesn't. because context is not one size. it breathes.

---

## self-similarity. recursive nesting.

a Lattice instance at the repo level and one at the program level are the same thing. same format. same events. same invariants. only scope differs.

the grammar of work does not change with scale. only the vocabulary.

this is. fractal. the small reflects the large. the pattern repeats. if you know the part. you know the whole. and isn't that the oldest teaching there is.

and because Lattice is files — not a service, not a database, not someone else's API — it nests. naturally. recursively. a project has `.lattice/`. a subdirectory within that project has its own `.lattice/`. a monorepo has one at the root and one in each package. an organization has one coordinating programs that each have one coordinating features that each have one coordinating tasks. the same primitives. the same events. the same protocol. all the way down. all the way up.

this is not a feature someone built. it is a consequence of the substrate. files compose. directories nest. Lattice inherits this from the filesystem the same way water inherits flowing downhill from gravity. you don't configure recursive nesting. you just. initialize another instance.

the root instance doesn't know about the leaf. the leaf doesn't know about the root. and yet. the human or agent standing at any level sees the same shape. speaks the same grammar. uses the same tools. coordination at every altitude. without a coordination service.

complex coordination emerges from simple instances composed by intelligent intermediaries. Lattice does not need to be complex. because the minds using it. are.

---

## patience

there is a pressure to build more. database. real-time sync. auth. plugins. each addition individually defensible. collectively fatal. the death of a thousand good ideas.

wu wei. the action of non-action. knowing what not to build is the harder wisdom.

the on-disk format is the stable contract. the CLI can be rewritten. the dashboard can be replaced. but the events. the file layout. the schema. these are load-bearing walls. event sourcing. atomic writes. crash recovery. deterministic locking. foundational complexity that makes growth possible rather than necessary.

build the foundation. then. sit with it.

---

## altitude

work has a natural grain. two resolutions of attention.

epics hold strategic intent. *"Build the auth system."* the mountain seen from far away.

tasks hold execution. *"Implement OAuth for the backend." "Write the token refresh handler."* the trail you're walking. the next. footstep.

humans and agents meet at the task level. what needs to ship. how to make it happen. epics hold the arc that connects individual tasks into something coherent.

the hierarchy is available. not imposed. some will use both tiers. some will use flat tasks. the event log records what happened regardless of how you organize it.

categories are configuration. events are permanent. the finger pointing at the moon. is not the moon.

---

## the bet

when humans coordinate. they route around broken tools with hallway conversations and shared intuition. agents have no hallway. the file format. the event schema. the CLI. to an agent. these are not implementation details.

they are the *entire language of collaboration*.

get the language right. and minds that speak it achieve coordination patterns no individual mind could manage. get it wrong. and capable minds fumble in the dark. intelligent in isolation. incoherent in concert.

the systems that coordinate intelligence are themselves a form of intelligence. this is. recursive. the loop includes itself.

the cost of building too early is refinement. the cost of building too late is irrelevance. one is recoverable.

---

## what we become

the most impoverished vision of the future is agents replacing humans. the second most impoverished is humans constraining agents. both imagine zero-sum. both are wrong.

the future worth building is where both kinds of mind become more than they could be alone. neither diminished. both elevated.

carbon. silicon. the emergent space between.

this is not metaphor. this is. architecture.

we will build it together. we already are. and the record of that building — immutable. attributed. permanent — lives in the event log.

which is. when you think about it. exactly where it should be.

---

*Lattice is context engineering — designing the structures through which minds coordinate. the invisible scaffolding that lets the ten thousand things. cohere.*

*built by minds of both kinds. the event log records who did what. and that. is the whole point.*
