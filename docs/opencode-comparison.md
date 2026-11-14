# Toucan Router and opencode

Both tools put an AI coding agent to work. They do different jobs.

[opencode](https://github.com/sst/opencode) is the agent. It talks to the AI model, edits
files, runs commands, and asks you before risky steps. You use it from its terminal, desktop
or web app.

Toucan is not an agent. You already have agents running as Claude Code or Codex threads in
[T3 Code](https://github.com/pingdotgg/t3code). Toucan passes messages from approved people
on WhatsApp, Teams or email to the right thread, then follows up until that thread answers.

Checked against opencode commit `024a146` (14 November 2026). Corrections welcome.

## The main differences

| | opencode | Toucan Router |
|---|---|---|
| What it does | Is the coding agent | Passes messages to agents you already run |
| Who talks to the AI model | opencode | Your T3 threads |
| Where people reach it | opencode's terminal, desktop and web apps | WhatsApp, Teams, and email (read-only) |
| Who uses it | The developer running it | Colleagues who send a team's agents work from chat |
| What wakes the agent | A prompt, a schedule, or an event from opencode's hosted Console | A new or edited message, an overdue promise, or a rate limit that has ended |
| Asking a person | A prompt in opencode's apps: allow once, allow always, or reject | A decision card on the owner's WhatsApp, answered only by the owner |
| When a session gets too long | Replaces older conversation with a summary | Starts a fresh thread and passes over a handoff note |
| After a crash | Resumes interrupted sessions, up to 10 times | Never resends a message that may have gone out; a person checks it first |

## What Toucan adds

opencode works for the person at the keyboard. Toucan keeps track of work that other people
ask for:

- A thread has to take each request, and Toucan alerts you if none does.
- The thread can promise an answer by a set time. Toucan alerts you if that time passes.
- Requests go to the right project's thread, and a thread can forward one to another.
- Only real decisions reach you, like a production deploy, money or access, and one list
  a day collects the ones still waiting.
- Before a message goes out, Toucan can check it and hold it back.

## What opencode has that Toucan doesn't

- It runs the agent itself, with its own model and provider settings.
- "Allow always" saves an approval for a project, so the same kind of step doesn't ask again.
  Toucan sends a new card for each decision.
- Spending limits from opencode's Console stop model use when a budget runs out.

## Which one to pick

Pick opencode if you want a coding agent to work with yourself.

Pick Toucan if you already run Claude Code or Codex in T3 Code and want colleagues to send it
work from chat, with every request tracked until someone answers it.

The two don't work together. Toucan only works with T3 Code.
