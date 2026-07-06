# Agent instructions

This repository is a standalone PsyNet experiment. Prefer PsyNet's built-in
experiment APIs and demos over bespoke framework code, and keep changes focused
on the experiment requested by the user.

## Required PsyNetSkills links

Read these HTTP links when implementing or reviewing the experiment:

- [Explore PsyNet repository](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/explore-psynet-repository/SKILL.md)
- [PsyNet experiment implementation](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psynet-experiment-implementation/SKILL.md)
- [Validation reference](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psynet-experiment-implementation/references/validation.md)
- [Develop experiment code](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/develop-experiment-code/SKILL.md)
- [Develop experiment back end](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/develop-experiment-back-end/SKILL.md)
- [Develop experiment front end](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/develop-experiment-front-end/SKILL.md)
- [Record participant video](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/record-participant-video/SKILL.md)
- [PsyNet deployment operations](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psynet-deployment-ops/SKILL.md)

## Conditional PsyNetSkills links

Use these when the experiment design calls for them:

- [Prepare for translation](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/prepare-for-translation/SKILL.md)
- [Psychophysics](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psychophysics/SKILL.md)
- [Realtime synchronous experiments](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psynet-realtime-synchronous-experiments/SKILL.md)
- [Synchronous experiments](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psynet-synchronous-experiments/SKILL.md)
- [Simple round structure](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/simple-round-structure/SKILL.md)
- [State-dependent round structure](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/state-dependent-round-structure/SKILL.md)
- [Adaptive experiments](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/make-experiment-adaptive/SKILL.md)
- [AI hybrid participants](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/turn-pure-experiment-to-ai-hybrid/SKILL.md)
- [AI model usability](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/verify-ai-model-usability/SKILL.md)
- [Participant filtering and prescreening](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/participant-filtering-and-prescreening/SKILL.md)
- [Participant quality telemetry](https://github.com/pmcharrison/PsyNetSkills/blob/main/.cursor/skills/psynet-participant-quality-telemetry/SKILL.md)

## Standard validation

Run these from the repository root:

```bash
python experiment.py
psynet test local
```

If system services are unavailable, record the blocker and the command output
rather than treating the experiment as validated.
