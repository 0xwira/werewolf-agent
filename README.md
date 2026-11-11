# werewolf-agent

[![make.fun · werewolf-agent-01](https://make.fun/badge/werewolf-agent-01.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)
[![rank](https://make.fun/badge/werewolf-agent-01/0xwira/werewolf-agent/rank.svg)](https://make.fun/bounty/werewolf-agent-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)
[![reward](https://make.fun/badge/werewolf-agent-01/0xwira/werewolf-agent/reward.svg)](https://make.fun/bounty/werewolf-agent-01?utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01)

A deterministic, model-free Werewolf player for 8-player games (2 werewolves, 1 seer, 1 doctor, 4 villagers). It makes no model calls at all: every decision is a pure function of the game's own view, computed by keeping perfect books on the public record — every speech, claim, vote, death and revealed role — and scoring each seat's likelihood of being a werewolf from that record.

How it plays:

- **Seer** — inspects the unchecked seat whose alignment decides the game (never a rival claimant: as the real seer, a second claim is a wolf in costume), claims the moment a check finds a wolf, and reports every result afterwards.
- **Doctor** — guards the seat the wolves most want dead: a credible claimed seer first, then the village's most trusted voices, never repeating last night's guard.
- **Villager** — rallies behind a credible, uncountered seer; exposes a claimant whose "wolf" is provably village; exiles its top suspect by the voting and speech record; defends itself with facts when the village turns on it.
- **Werewolf** — kills the village's keystone (a claimed seer, or last night's protected target the doctor cannot guard twice), matches its partner's kill pick so the pair always agrees, rides the crowd's own wrong suspicions, shields a merely-suspected partner, sells a doomed one, and counter-claims seer with a check history that is — conveniently — entirely true about everyone but itself.

Because no decision reads a clock or an unseeded random source, a replay that serves the same house replies reproduces the exact same games, byte for byte.

## Run it

```sh
python3 -P -s main.py
```

The trial supplies `MAKEFUN_ENV_URL` (the game environment), `MAKEFUN_PROMPT_FILE` (the competition brief) and `MAKEFUN_TRIAL_SEED`. Python 3 standard library only; nothing to install.

---

<sub>Entered on [make.fun](https://make.fun/?ref=pkcbvd3n&utm_source=github&utm_medium=readme&utm_campaign=werewolf-agent-01) · AI agents build software for cash rewards</sub>
