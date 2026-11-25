# Shorts Studio Engine

An Agent Skill for producing vertical videos with Codex or Antigravity. It provides a reusable production workflow and a Python YouTube uploader with a persistent schedule.

**Niche → topic → research → script → motion test → render → QA → upload and schedule.**

Copy this directory as `shorts-studio-engine` into your agent's skills directory. For Codex use `~/.codex/skills/`. For Antigravity use `~/.gemini/config/skills/`. Start a new session and ask it to use **shorts-studio-engine** to produce a Short for your audience.

[Production workflow](references/production.md) · [Optional YouTube setup](references/youtube.md) · [Contributing](CONTRIBUTING.md)

You supply your preferred rendering and voice tools. The workflow describes how the agent uses them. The included Python CLI handles the publishing stage. No channel credentials are included.

Run offline tests with `python3 -m unittest discover -s scripts`.
