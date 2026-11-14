# Toucan Router FAQ

## Is Toucan an agent harness?

No.

A harness is what turns a language model into an agent. The model can only write text. The
harness gives it tools it can call with that text (read a file, edit a file, run a command),
a system prompt and context, and runs the loop that carries out each call. Claude Code, Codex,
opencode and TrueForge are harnesses, or include one.

Toucan never calls a model. It has no system prompt, no tools for a model and no agent loop. It
is a command-line tool and a timer. It reads approved chats on WhatsApp, Teams and email, works
out which [T3 Code](https://github.com/pingdotgg/t3code) thread owns each message, wakes that
thread, and tracks the request until the thread answers it.

The agent in that thread is whatever harness you already run there, usually Claude Code or
Codex. It reads requests and sends replies by running `toucan` commands through its own shell,
the same way it runs `git`.

## Where does Toucan sit, then?

One layer above T3 Code:

```
People on WhatsApp, Teams, email
        |
Toucan Router      reads approved chats, wakes the owner thread, tracks each request
        |
T3 Code            the app you run your threads in; it starts and talks to the harness
        |
Harness            Claude Code, Codex: tools, system prompt, the agent loop
        |
Model              Claude, GPT, ...
```

Toucan talks only to T3 Code. It wakes a thread through a T3 webhook task and reads T3's
database to find the live thread and see whether its provider is rate-limited. It never talks
to the harness or the model.

## Isn't T3 Code a harness?

No. Theo, who created T3 Code, explains this in
[My last video about ai harnesses](https://www.youtube.com/watch?v=ebILGCoN4UU)
(30 October 2026).

T3 Code is the interface. You install Claude Code or Codex yourself, and T3 Code runs it with
your own subscription, skills and plugins. Picking a model in T3 Code really picks a harness.
In Theo's words, "T3 Code isn't a kitchen, T3 Code is a food court at a mall where you pick
which kitchen you go to."

What T3 Code adds is an MCP server that every thread gets, whichever harness it uses. A thread
can use it to start a thread or subagent on another harness, send a message to another thread,
search past threads, or schedule a task. Theo calls T3 Code an ADE, an agentic development
environment.

Toucan builds on that. The harness does the work, T3 Code runs the threads, and Toucan brings
other people's requests to the right thread.

## Why not build a harness and skip T3 Code?

Because the harness you already use is good, and keeps getting better. Through T3 Code, Toucan:

- uses the agent you already pay for, with your subscription, skills, plugins, model and
  permission settings;
- lets each project's thread run on whichever harness suits it, and hands requests to a
  standby thread on another provider while one is rate-limited;
- gets every improvement to Claude Code, Codex or T3 Code without any change to Toucan.

A harness of our own would mean a new agent to trust, configure and pay for. Toucan's job is
the part no harness does: making sure each request from a colleague gets taken, answered,
followed up when promised, or escalated to you.

## How is it different from OpenClaw, NanoClaw, Hermes Agent, TrueForge or opencode?

Each of these runs the agent. Toucan passes work to agents you already run.

| | What it is | Runs the agent itself | Where people reach it |
|---|---|---|---|
| [OpenClaw](https://github.com/openclaw/openclaw) | A personal AI assistant on your own machine. Claude, Codex or local models plug in as its harness. | Yes | Its own apps, and over 20 chat apps |
| [NanoClaw](https://github.com/nanocoai/nanoclaw) | A smaller OpenClaw alternative that runs each agent in a container, on Anthropic's Agent SDK | Yes | WhatsApp, Telegram, Slack, Teams, email and more |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | An agent with its own memory and model sign-ins | Yes | About 25 chat apps |
| [TrueForge](https://github.com/truefoundry/trueforge) | An open-source harness: a server, an HTTP API and a chat UI | Yes | Its chat UI and API |
| [opencode](https://github.com/anomalyco/opencode) | A coding agent | Yes | Its terminal, desktop and web apps |
| Toucan Router | A router for T3 Code threads | No. Your Claude Code or Codex threads do. | WhatsApp, Teams, and email (read-only) |

Pick one of the others if you want a single assistant that people talk to directly and you
don't run an agent yet.

Pick Toucan if you already run Claude Code or Codex in T3 Code and want colleagues to send it
work from chat, with every request tracked until someone answers it.

Longer comparisons: [Hermes Agent](hermes-comparison.md), [opencode](opencode-comparison.md).

## Does Toucan add tools or an MCP server to my agent?

No. The thread uses its own shell to run the `toucan` command: `toucan inbox` to read requests,
then `claim`, `reply`, `escalate` and the rest. Toucan's wake-up call carries only counts and
times, never message text. The thread reads the text with `toucan inbox`, which marks it as
untrusted content from other people.

## Which harnesses does it work with?

Claude Code and Codex, through T3 Code. `toucan doctor` checks that at least one is installed,
and Toucan reads the rate-limit messages both of them give. Those are the only two we have
tested. Toucan doesn't work without T3 Code.

Descriptions of the other tools checked on 14 November 2026. Corrections welcome.
