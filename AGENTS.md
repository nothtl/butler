# Butler — agent instructions

## HARD RULE: no hardcoded replies

Butler must never answer the user with a prewritten/canned string.

- Every user-facing conversational reply MUST be generated live by the LLM
  (`butler/chat.py`) from **live state** gathered at request time
  (calendar, events, tasks, courses, files, presence, etc.).
- Do NOT add small-talk dictionaries, greeting strings, "I don't know"
  fallbacks, or template sentences that impersonate an answer.
- Structured command output (rendering real rows such as a task list or a
  plan) is allowed because it is data, not a canned reply.
- If the LLM is not configured/unreachable, return the live state itself
  (data), not an invented sentence.
- The only permitted fixed strings are system/error notices that describe an
  actual failure (e.g. "LLM not configured"), and they must be clearly
  system-level, never disguised as a conversational reply.

If a feature seems to need a canned reply, instead:
1. gather the relevant live state,
2. pass it to the LLM,
3. let the model phrase the answer.

## Context

- Butler is a local assistant (Telegram + CLI) over the user's own data.
- LLM: DeepSeek via `[ai]` in `~/.config/butler/config.toml`
  (`BUTLER_LLM_KEY`, `base_url`, `model`).
- Bot runs as the user service `butler-bot.service`; restart after changes:
  `systemctl --user restart butler-bot`.
