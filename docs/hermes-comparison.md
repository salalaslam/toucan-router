# Toucan Router and Hermes Agent

Both tools let people message an AI agent from WhatsApp, Teams or email. They do
different jobs.

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is the agent. It talks to the
AI model, runs the tools, keeps memory, and connects to about 25 chat apps.

Toucan is not an agent. You already have agents running as Claude Code or Codex threads in
[T3 Code](https://github.com/pingdotgg/t3code). Toucan passes messages from approved people
to the right thread, then follows up until that thread answers.

Checked against Hermes Agent commit `dfd98fc1` (12 November 2026). Corrections welcome.

## The main differences

| | Hermes Agent | Toucan Router |
|---|---|---|
| What it does | Is the agent | Passes messages to agents you already run |
| Who talks to the AI model | Hermes | Your T3 threads |
| Chat apps | About 25 | WhatsApp, Teams, and email (read-only) |
| How it sees a message | Part of an ongoing chat | A request someone has to answer |
| Who is allowed to write | Approved lists, plus one-time pairing codes | Approved list of phone numbers, Teams accounts and email addresses |
| When a chat gets too long | Shortens the old part into a summary | Starts a fresh thread and passes over a handoff note |
| AI accounts | Signs in to AI providers itself | Uses whatever your agents already use |

## What Toucan adds

Hermes answers each message as it arrives. Toucan keeps track of every request:

- A thread has to take each request.
- The thread can promise an answer by a set time. Toucan alerts you if that time passes.
- Only real decisions reach you, like a production deploy, money or access.
- Before a message goes out, Toucan can check it and hold it back.

Hermes doesn't do any of this.

## Which one to pick

Pick Hermes if you want one agent that people talk to directly and you don't have one yet.

Pick Toucan if you already run Claude Code or Codex in T3 Code and want colleagues to
send it work from chat, with every request tracked until someone answers it.

The two don't work together. Toucan only works with T3 Code.
