#!/usr/bin/env python3
"""Werewolf agent for make.fun's werewolf-agent-01 bounty.

A deterministic, model-free player: standard library only, no model calls,
no clock, no unseeded randomness. Every decision is a pure function of the
game's own view (the public log plus this seat's private information), so a
replay that serves the same house replies produces the exact same play.

Strategy in one paragraph: keep perfect books on every game (votes, speeches,
claims, deaths and revealed roles), score every seat's likelihood of being a
werewolf from that record, and play each role to the score: the seer checks
the seats whose alignment decides the game and claims with a found wolf, the
doctor guards the village's most valuable seat, the village exiles its top
suspect and rallies behind a credible seer, and the wolves kill the village's
most trusted voices, back the village's own wrong suspicions, and sell a
doomed partner only when the sale buys real cover.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED", "0")
ENV_URL = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")


def seeded(*parts):
    """A deterministic float in [0, 1) from the trial seed and the parts."""
    h = hashlib.sha256("|".join([TRIAL_SEED] + [str(p) for p in parts]).encode())
    return int.from_bytes(h.digest()[:8], "big") / float(1 << 64)


def pick(options, *parts):
    """Deterministically pick one element of a non-empty sorted list."""
    if not options:
        return None
    return options[int(seeded(*parts) * len(options)) % len(options)]


def fold(text):
    """Fold a statement the env's way and cap it well under the limit."""
    text = re.sub(r"[\t\n\v\f\r\u2028\u2029]+", " ", text)
    text = "".join(ch for ch in text if ch >= " " or ch == " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 480:
        text = text[:480].rstrip()
    return text


# ---------------------------------------------------------------------------
# Env client: one request at a time, in a fixed order
# ---------------------------------------------------------------------------


def env_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        ENV_URL + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


# ---------------------------------------------------------------------------
# Reading a game view into a state the strategy can reason about
# ---------------------------------------------------------------------------

WOLF_SIDE = {"werewolf"}
VILLAGE_SIDE = {"villager", "seer", "doctor"}


class Game:
    def __init__(self, view):
        self.view = view
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "")
        you = view.get("you") or {}
        self.me = you.get("seat")
        self.my_name = you.get("name")
        self.my_role = you.get("role")
        self.partner = (you.get("partner") or {}).get("seat")
        self.players = {p["seat"]: p for p in view.get("players") or []}
        self.names = {p["seat"]: p.get("name", "seat %s" % p["seat"]) for p in self.players.values()}
        self.name_to_seat = {}
        for seat, name in self.names.items():
            if name:
                self.name_to_seat[name.lower()] = seat
        self.alive = sorted(s for s, p in self.players.items() if p.get("alive"))
        self.revealed = {s: p.get("role") for s, p in self.players.items() if p.get("role")}
        self.log = view.get("log") or []
        self.seer_results = view.get("seerResults") or []
        self.last_protect = view.get("lastProtect") or {}
        self.wolf_votes = view.get("wolfVotes") or []
        # Parsed public record
        self.speeches = []       # {day, round, seat, text}
        self.votes = []          # {day, seat, target}
        self.deaths = []         # {day, seat, role, how: dawn|exile}
        self.exiles = []         # {day, seat, role}
        self.dawn = []           # {day, died, role}
        self._parse_log()
        self.claims = self._parse_claims()
        self.my_claims = self.claims.get(self.me, {})

    # -- log ---------------------------------------------------------------
    def _parse_log(self):
        for e in self.log:
            kind = e.get("kind")
            if kind == "speech":
                self.speeches.append(
                    {"day": e.get("day"), "round": e.get("round"), "seat": e.get("seat"), "text": e.get("text") or ""}
                )
            elif kind == "vote":
                self.votes.append({"day": e.get("day"), "seat": e.get("seat"), "target": e.get("target")})
            elif kind == "dawn":
                self.dawn.append({"day": e.get("day"), "died": e.get("died"), "role": e.get("role")})
                if e.get("died"):
                    self.deaths.append({"day": e.get("day"), "seat": e.get("died"), "role": e.get("role"), "how": "dawn"})
            elif kind == "exile":
                self.exiles.append({"day": e.get("day"), "seat": e.get("seat"), "role": e.get("role")})
                self.deaths.append({"day": e.get("day"), "seat": e.get("seat"), "role": e.get("role"), "how": "exile"})

    # -- claims in speeches --------------------------------------------------
    def _seat_refs(self, text):
        """Seats a text refers to, by 'seat N' or by a player's name."""
        found = []
        low = " " + text.lower() + " "
        for m in re.finditer(r"seat\s*(\d)", low):
            s = int(m.group(1))
            if s in self.players and s not in found:
                found.append(s)
        for name, seat in self.name_to_seat.items():
            if name and re.search(r"\b" + re.escape(name) + r"\b", low) and seat not in found:
                found.append(seat)
        return found

    def _parse_claims(self):
        """Extract role claims and claimed night results from every speech.

        Returns {seat: {"seer": bool, "doctor": bool,
                        "reports": [(target, is_wolf)], "accuse": [seats],
                        "defend": [seats], "vote_for": [seats]}}.
        """
        out = {}
        for sp in self.speeches:
            seat = sp["seat"]
            text = sp["text"]
            low = text.lower()
            c = out.setdefault(seat, {"seer": False, "doctor": False, "reports": [], "accuse": [], "defend": [], "vote_for": []})
            if re.search(r"\bi(?:'m| am) (the )?seer\b", low) or re.search(r"\bi claim seer\b", low) or re.search(r"\bas your seer\b", low):
                c["seer"] = True
            if re.search(r"\bi(?:'m| am) (the )?doctor\b", low) or re.search(r"\bi claim doctor\b", low):
                c["doctor"] = True
            # Judge each reference inside its own sentence: a speech often
            # reports one check and accuses someone else in the next breath.
            sentences = [s.strip() for s in re.split(r"[.!?;]+", low) if s.strip()]
            for seg in sentences:
                seg_refs = [r for r in self._seat_refs(seg) if r != seat]
                for r in seg_refs:
                    if any(w in seg for w in ("checked", "inspected", "i saw", "visited")) and (
                        "werewolf" in seg or "wolf" in seg or "clean" in seg or "not a" in seg or "innocent" in seg
                    ):
                        if any(w in seg for w in ("not a werewolf", "not a wolf", "clean", "innocent", "not guilty")):
                            c["reports"].append((r, False))
                        elif "werewolf" in seg or re.search(r"\bwolf\b", seg):
                            c["reports"].append((r, True))
                        continue
                    if any(phrase in seg for phrase in ("is a werewolf", "is the werewolf", "is a wolf")):
                        if r not in c["accuse"]:
                            c["accuse"].append(r)
                        if c["seer"]:
                            c["reports"].append((r, True))
                    elif any(phrase in seg for phrase in ("is not a werewolf", "is not a wolf", "is clean", "is innocent")):
                        if r not in c["defend"]:
                            c["defend"].append(r)
                        if c["seer"]:
                            c["reports"].append((r, False))
                    elif re.search(r"\b(trust|believe|defend|protect|vouch)\b", seg) and r not in c["defend"]:
                        c["defend"].append(r)
                    elif re.search(r"\b(suspect|distrust|suspicious|untrust|exile|lynch)\b", seg) and r not in c["accuse"]:
                        c["accuse"].append(r)
            for r in self._seat_refs(text):
                if r != seat and re.search(r"\b(vote|voting)\b", low) and r not in c["vote_for"]:
                    c["vote_for"].append(r)
        return out

    # -- knowledge -----------------------------------------------------------
    def known_wolves(self):
        """Seats I know for certain are werewolves (private truth only)."""
        out = set()
        if self.my_role == "werewolf":
            out.add(self.me)
            if self.partner:
                out.add(self.partner)
        if self.my_role == "seer":
            for r in self.seer_results:
                if r.get("werewolf"):
                    out.add(r.get("seat"))
        for seat, role in self.revealed.items():
            if role in WOLF_SIDE:
                out.add(seat)
        return out

    def known_village(self):
        """Seats I know for certain are village-side (private truth only)."""
        out = set()
        if self.my_role in VILLAGE_SIDE:
            out.add(self.me)
        if self.my_role == "seer":
            for r in self.seer_results:
                if not r.get("werewolf"):
                    out.add(r.get("seat"))
        if self.my_role == "werewolf":
            pass  # a wolf knows only the wolves; everyone else is a target
        for seat, role in self.revealed.items():
            if role in VILLAGE_SIDE:
                out.add(seat)
        return out

    def seer_claimants(self):
        return sorted(s for s in self.alive if self.claims.get(s, {}).get("seer"))

    def credible_seer(self):
        """The single seer claimant I have no proof against, or None."""
        claimants = self.seer_claimants()
        if self.my_role == "seer":
            others = [s for s in claimants if s != self.me]
            return self.me if not others else None
        believable = []
        kv = self.known_village()
        kw = self.known_wolves()
        for s in claimants:
            fake = False
            for target, is_wolf in self.claims.get(s, {}).get("reports", []):
                if is_wolf and target in kv:
                    fake = True
                if not is_wolf and target in kw:
                    fake = True
            if not fake:
                believable.append(s)
        if len(believable) == 1:
            return believable[0]
        return None

    def public_credible_seer(self):
        """The seer claimant the village itself would believe: a single
        claimant whose reports never contradict a revealed role."""
        claimants = self.seer_claimants()
        believable = []
        for s in claimants:
            fake = False
            for target, is_wolf in self.claims.get(s, {}).get("reports", []):
                role = self.revealed.get(target)
                if role is not None and (role in WOLF_SIDE) != is_wolf:
                    fake = True
            if not fake:
                believable.append(s)
        return believable[0] if len(believable) == 1 else None

    def suspicion(self, public_only=False):
        """Score every seat: higher means more likely a werewolf.

        Built from the public record plus, unless public_only, my private
        certainties (a wolf reads the crowd with public_only; a villager
        acts on everything it knows).
        """
        score = {s: 0.0 for s in self.players}
        dead_role = {d["seat"]: d["role"] for d in self.deaths}
        # Voting record against revealed roles
        for v in self.votes:
            voter, target = v["seat"], v["target"]
            if target is None or voter not in score:
                continue
            role = dead_role.get(target)
            if role in VILLAGE_SIDE:
                score[voter] += 1.6
            elif role in WOLF_SIDE:
                score[voter] -= 1.6
        # Speech record against revealed roles
        for seat, c in self.claims.items():
            if seat not in score:
                continue
            for t in c["accuse"]:
                if dead_role.get(t) in WOLF_SIDE:
                    score[seat] -= 1.2
                elif dead_role.get(t) in VILLAGE_SIDE:
                    score[seat] += 1.2
            for t in c["defend"]:
                if dead_role.get(t) in WOLF_SIDE:
                    score[seat] += 1.8
                elif dead_role.get(t) in VILLAGE_SIDE:
                    score[seat] -= 0.8
        # Seer claims and their reports
        claimants = self.seer_claimants()
        cred = self.public_credible_seer() if public_only else self.credible_seer()
        for s in claimants:
            if s == cred:
                score[s] -= 1.2
            elif len(claimants) > 1:
                score[s] += 1.0
            for target, is_wolf in self.claims.get(s, {}).get("reports", []):
                if target not in score:
                    continue
                if s == cred:
                    score[target] += 4.5 if is_wolf else -2.0
                else:
                    score[target] += 1.8 if is_wolf else -0.7
        # Proven-fake claims: privately I know more; the public board only
        # ever learns from revealed roles.
        if public_only:
            kv = {s for s, r in self.revealed.items() if r in VILLAGE_SIDE}
            kw = {s for s, r in self.revealed.items() if r in WOLF_SIDE}
        else:
            kv, kw = self.known_village(), self.known_wolves()
        for s in claimants:
            for target, is_wolf in self.claims.get(s, {}).get("reports", []):
                if (is_wolf and target in kv) or (not is_wolf and target in kw):
                    score[s] += 6.0
        # The credible seer's soft reads carry weight too
        if cred is not None:
            for target in self.claims.get(cred, {}).get("accuse", []):
                if target in score:
                    score[target] += 1.5
        # Herding and certainty theatre: weak but real signals
        accusers = {}
        for seat, c in self.claims.items():
            for t in c.get("accuse", []):
                accusers.setdefault(t, set()).add(seat)
        for t, who in accusers.items():
            if t in score and len(who) >= 2:
                score[t] += 0.7
        for seat, c in self.claims.items():
            if seat in score and not c.get("seer"):
                strong = [t for t in c.get("accuse", []) if t in score]
                if strong and any(
                    "is a werewolf" in (sp["text"] or "").lower() or "is the werewolf" in (sp["text"] or "").lower()
                    for sp in self.speeches if sp["seat"] == seat
                ):
                    score[seat] += 0.6
        if not public_only:
            # My own certainties dominate everything
            for s in kw:
                score[s] = 99.0
            for s in kv:
                score[s] = -99.0
        # The quiet drift wolf-ward as days pass
        spoke = {sp["seat"] for sp in self.speeches}
        if self.day >= 2:
            for s in self.alive:
                if s not in spoke and s != self.me:
                    score[s] += 0.5
        # Players who pushed me while I am village-side earn suspicion
        if not public_only and self.my_role in VILLAGE_SIDE:
            for s in self.alive:
                if s == self.me:
                    continue
                c = self.claims.get(s, {})
                if self.me in c.get("accuse", []) or self.me in c.get("vote_for", []):
                    score[s] += 1.2
                for v in self.votes:
                    if v["seat"] == s and v["target"] == self.me:
                        score[s] += 0.8
        return score


# ---------------------------------------------------------------------------
# Night and vote decisions
# ---------------------------------------------------------------------------


def decide_inspect(g, options):
    """Seer: inspect the unchecked seat whose alignment decides the game."""
    checked = {r.get("seat") for r in g.seer_results}
    score = g.suspicion()
    best, best_val = None, None
    for s in sorted(options):
        if s == g.me or s in checked:
            continue
        val = score.get(s, 0.0)
        if s in g.seer_claimants():
            # I am the seer: another claimant is a wolf in seer's clothing.
            # I do not need a check to know it, so spend the check elsewhere.
            val -= 5.0
        # inspecting today's likely exile wastes the check: the exile reveals it
        top_exile = max((x for x in g.alive if x != g.me), key=lambda x: score.get(x, 0.0), default=None)
        if s == top_exile and score.get(s, 0.0) > 3.0:
            val -= 3.0
        val += seeded("inspect", g.day, s) * 0.3
        if best_val is None or val > best_val:
            best, best_val = s, val
    return best


def decide_protect(g, options):
    """Doctor: guard the seat the wolves most want dead."""
    score = g.suspicion()
    cred = g.credible_seer()
    best, best_val = None, None
    for s in sorted(options):
        if s == g.me:
            continue
        val = -score.get(s, 0.0) * 0.6  # the trusted are the targets
        if s == cred:
            val += 10.0
        # whoever the village pushed hardest yesterday survived a scare: wolves noticed too
        pushed = sum(1 for v in g.votes if v["target"] == s and v["day"] == g.day - 1)
        val += pushed * 0.8
        val += seeded("protect", g.day, s) * 0.3
        if best_val is None or val > best_val:
            best, best_val = s, val
    return best


def decide_kill(g, options):
    """Werewolf: agree with my partner, or pick the village's keystone."""
    tonight = g.day
    # Second wolf to act: the kill needs agreement, so match my partner's pick.
    for wv in g.wolf_votes:
        if wv.get("night") == tonight and wv.get("seat") == g.partner and wv.get("target") in options:
            return wv["target"]
    score = g.suspicion()
    cred = g.credible_seer()
    # If last night's agreed target survived, the doctor shielded them and
    # cannot shield them again tonight: strike the same seat.
    repeat = None
    if g.dawn and g.dawn[-1].get("died") is None and tonight >= 2:
        prev = [wv for wv in g.wolf_votes if wv.get("night") == tonight - 1]
        targets = {wv.get("target") for wv in prev if wv.get("target")}
        if len(targets) == 1:
            t = targets.pop()
            if t in options:
                repeat = t
    best, best_val = None, None
    for s in sorted(options):
        if s == g.me or s == g.partner:
            continue
        val = -score.get(s, 0.0) * 0.8  # kill the trusted, not the suspected
        if s == cred:
            val += 10.0
        elif s in g.seer_claimants():
            val += 5.0
        if s == repeat:
            val += 8.0
        # a seat the village is about to exile itself is a wasted kill
        if score.get(s, 0.0) > 3.5:
            val -= 4.0
        val += seeded("kill", g.day, s) * 0.3
        if best_val is None or val > best_val:
            best, best_val = s, val
    return best


def decide_vote(g, options):
    """Vote with the brain of the role I hold."""
    score = g.suspicion()
    me = g.me
    opts = [s for s in sorted(options) if s != me]
    if not opts:
        return None

    if g.my_role == "werewolf":
        partner = g.partner
        pub = g.suspicion(public_only=True)
        non_wolves = [s for s in opts if s != partner]
        if not non_wolves:
            return partner
        # the village's own top suspect among non-wolves: blend with the crowd
        ranked = sorted(non_wolves, key=lambda s: -pub.get(s, 0.0))
        crowd = ranked[0]
        if partner in opts:
            p_score = pub.get(partner, 0.0)
            second = pub.get(ranked[1], 0.0) if len(ranked) > 1 else 0.0
            pcred = g.public_credible_seer()
            named_by_seer = pcred is not None and (partner, True) in g.claims.get(pcred, {}).get("reports", [])
            partner_is_top = all(p_score >= pub.get(s, 0.0) for s in opts if s != partner)
            # bus only a doomed partner; otherwise shield them with my vote
            if named_by_seer or (partner_is_top and p_score > second + 2.5):
                return partner
            if partner_is_top:
                return crowd
        return crowd

    # Village-side: a proven liar outranks everything
    kv, kw = g.known_village(), g.known_wolves()
    for s in opts:
        if s in kw:
            return s
    cred = g.credible_seer()
    if cred is not None and cred != me:
        for target, is_wolf in g.claims.get(cred, {}).get("reports", []):
            if is_wolf and target in opts:
                return target
    ranked = sorted(opts, key=lambda s: -score.get(s, 0.0))
    top = ranked[0]
    # never exile my certain-clean or the credible seer while any other choice exists
    if (top in kv or top == cred) and len(ranked) > 1:
        return ranked[1]
    return top


# ---------------------------------------------------------------------------
# Speech: short, factual, consistent with everything I have said before
# ---------------------------------------------------------------------------


def nm(g, seat):
    return g.names.get(seat, "seat %s" % seat)


def my_speech_count(g):
    return sum(1 for sp in g.speeches if sp["seat"] == g.me)


def accusation_line(g, target, score):
    """One factual sentence against my top suspect."""
    facts = []
    for v in g.votes:
        if v["seat"] == target:
            role = g.revealed.get(v["target"])
            if role in VILLAGE_SIDE:
                facts.append("seat %s voted to exile %s, who was village" % (target, nm(g, v["target"])))
                break
    defended_wolf = None
    for t in g.claims.get(target, {}).get("defend", []):
        if g.revealed.get(t) in WOLF_SIDE:
            defended_wolf = t
    if defended_wolf is not None:
        facts.append("%s defended %s, who flipped werewolf" % (nm(g, target), nm(g, defended_wolf)))
    cred = g.public_credible_seer()
    if cred is not None and cred != g.me and target in g.claims.get(cred, {}).get("accuse", []):
        facts.append("our seer's own read points at %s" % nm(g, target))
    pushed = sum(1 for v in g.votes if v["seat"] == target)
    templates = [
        "I distrust {t}. {f}I will vote {t} today unless the seer has something better.",
        "My vote is going to {t}. {f}The record is the best guide we have.",
        "{t} is my top suspect. {f}I am voting there.",
    ]
    f = (facts[0][0].upper() + facts[0][1:] + ". ") if facts else ""
    line = pick(templates, "accuse", g.day, my_speech_count(g), target).format(t=nm(g, target), f=f)
    return fold(line)


def seer_reveal_line(g):
    parts = ["I am the seer."]
    for r in g.seer_results:
        t = r.get("seat")
        if r.get("werewolf"):
            parts.append("Night %s I checked %s: werewolf." % (r.get("night"), nm(g, t)))
        else:
            parts.append("Night %s I checked %s: not a werewolf." % (r.get("night"), nm(g, t)))
    wolves = [r.get("seat") for r in g.seer_results if r.get("werewolf") and r.get("seat") in g.alive]
    if wolves:
        parts.append("Vote %s today." % nm(g, wolves[0]))
    return fold(" ".join(parts))


def should_reveal_seer(g):
    """Claim when I hold a live wolf, or when silence is about to kill me."""
    if g.my_role != "seer":
        return False
    if g.my_claims.get("seer"):
        return True  # already claimed: keep reporting
    found = [r for r in g.seer_results if r.get("werewolf") and r.get("seat") in g.alive]
    if found:
        # A found wolf is claimed at once: the information dies with me
        # otherwise, and the doctor can guard a claimed seer tonight.
        return True
    my_heat = sum(1 for s in g.alive if g.me in g.claims.get(s, {}).get("accuse", []) + g.claims.get(s, {}).get("vote_for", []))
    if my_heat >= 2 and g.seer_results:
        return True  # about to be exiled: take my results public first
    if g.day >= 3 and len(g.seer_results) >= 2:
        return True  # late game: my clean list narrows the wolf pool
    return False


def decide_speak(g, rnd):
    """Return a statement, or None to pass. `rnd` counts my speeches so far."""
    me = g.me
    score = g.suspicion()
    kv, kw = g.known_village(), g.known_wolves()

    # 1) A "seer" has named a seat I know is village: expose the liar at once.
    if g.my_role in VILLAGE_SIDE:
        for s in g.seer_claimants():
            if s == me:
                continue
            for target, is_wolf in g.claims.get(s, {}).get("reports", []):
                if is_wolf and target in kv:
                    return fold(
                        "%s claims seer and calls %s a werewolf. That is a lie I can prove from my own seat: "
                        "%s is village. A real seer never gets that wrong, so %s is the werewolf. Vote %s."
                        % (nm(g, s), nm(g, target), nm(g, target), nm(g, s), nm(g, s))
                    )

    # 2) Seer play: reveal on schedule, then report every morning after.
    if g.my_role == "seer" and should_reveal_seer(g):
        if rnd == 0 or not g.my_claims.get("seer"):
            return seer_reveal_line(g)
        last = g.seer_results[-1] if g.seer_results else None
        if last:
            t = last.get("seat")
            verdict = "werewolf" if last.get("werewolf") else "not a werewolf"
            return fold("Seer report: last night I checked %s: %s. Act on it." % (nm(g, t), verdict))

    # 3) A wolf named by the real seer counter-claims with a consistent fake history.
    if g.my_role == "werewolf":
        cred = g.credible_seer()
        named = cred is not None and any(
            (me, True) in g.claims.get(cred, {}).get("reports", []) for _ in [0]
        )
        already = g.my_claims.get("seer")
        if named or already:
            if not already:
                # fabricate checks on two living non-wolves, fixed by the seed
                others = [s for s in g.alive if s not in (me, g.partner)][:2]
                a = others[0] if others else None
                b = others[1] if len(others) > 1 else a
                return fold(
                    "I am the real seer, and %s is lying. Night 1 I checked %s: not a werewolf. "
                    "Last night I checked %s: not a werewolf. Do not let a fake seer lead this village."
                    % (nm(g, cred), nm(g, a), nm(g, b))
                )
            return fold("I stand by my claim: %s is the fake. Vote there, not on the real seer." % nm(g, cred))

    # 4) Doctor about to be exiled claims to survive the day.
    score_rank = sorted((s for s in g.alive if s != me), key=lambda s: -score.get(s, 0.0))
    heat_on_me = sum(
        1 for s in g.alive if s != me and (me in g.claims.get(s, {}).get("accuse", []) or me in g.claims.get(s, {}).get("vote_for", []))
    )
    if g.my_role == "doctor" and heat_on_me >= 2 and not g.my_claims.get("doctor"):
        return fold("Do not exile me: I am the doctor. I have been guarding this village every night. Aim somewhere else today.")

    # 5) Back a credible seer (when I am not the seer myself).
    cred = g.credible_seer()
    if cred is not None and cred != me and g.my_role != "werewolf":
        for target, is_wolf in g.claims.get(cred, {}).get("reports", []):
            if is_wolf and target in g.alive:
                return fold("The seer's report is the best lead we have: %s is named as a werewolf. I am voting %s." % (nm(g, target), nm(g, target)))

    # 6) Defence when the village turns on me.
    if heat_on_me >= 2 and rnd >= 1:
        alt = score_rank[0] if score_rank else None
        line = "I am village, and exiling me wastes a day the wolves will spend eating us. "
        if alt is not None:
            line += "Look at %s instead: the record against them is stronger." % nm(g, alt)
        return fold(line)

    # 7) A wolf speaks from the public board: ride the crowd's suspicion,
    #    and quietly steer it off my partner when it lands there.
    if g.my_role == "werewolf":
        pub = g.suspicion(public_only=True)
        pub_rank = sorted((s for s in g.alive if s != me), key=lambda s: -pub.get(s, 0.0))
        non_wolf_rank = [s for s in pub_rank if s != g.partner]
        if pub_rank and pub_rank[0] == g.partner and non_wolf_rank:
            target = non_wolf_rank[0]
            return fold(
                "I am not convinced about %s yet, and rushing helps only the wolves. %s worries me more. "
                "I would rather vote %s." % (nm(g, g.partner), nm(g, target), nm(g, target))
            )
        if non_wolf_rank:
            top = non_wolf_rank[0]
            if pub.get(top, 0.0) >= 0.8 or g.day >= 2:
                return accusation_line(g, top, pub)
            if rnd == 0:
                return fold("No certain read yet. I am watching who pushes easy targets: that is where wolves hide.")
        return None

    # 8) Default: press my top suspect, skipping seats I know are clean.
    candidates = [s for s in score_rank if s not in kv]
    if candidates:
        top = candidates[0]
        if score.get(top, 0.0) >= 0.8 or g.day >= 2:
            return accusation_line(g, top, score)
        if rnd == 0:
            died = g.dawn[-1] if g.dawn else None
            if died and died.get("died"):
                return fold("Losing %s hurts. I have no certain read yet, so I am watching the votes today: wolves hide inside easy bandwagons." % nm(g, died["died"]))
            return fold("Quiet night tells us little. I will judge by the voting record, not by who talks loudest.")
    if rnd == 0:
        return fold("I have no strong read yet. I am listening, and I will vote on the record, not on noise.")
    return None


# ---------------------------------------------------------------------------
# The run: answer one decision at a time, in game order, until done
# ---------------------------------------------------------------------------


def answer(pending):
    """Answer one pending decision; returns the /act body."""
    game_no = pending.get("game")
    kind = pending.get("decision")
    view = pending.get("view") or {}
    g = Game(view)
    if kind == "speak":
        text = decide_speak(g, my_speech_count(g))
        if text:
            return {"game": game_no, "decision": "speak", "text": text}
        return {"game": game_no, "decision": "speak", "pass": True}
    options = pending.get("options") or []
    if kind == "inspect":
        target = decide_inspect(g, options)
    elif kind == "protect":
        target = decide_protect(g, options)
    elif kind == "kill":
        target = decide_kill(g, options)
    elif kind == "vote":
        target = decide_vote(g, options)
    else:
        target = None
    if target is not None and options and target not in options:
        target = None
    return {"game": game_no, "decision": kind, "target": target}


def main():
    if not ENV_URL:
        print("MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 1500  # safety only; never sent anywhere
    failures = 0
    while time.monotonic() < deadline:
        status, obs = env_post("/observe", {})
        if status != 200 or not isinstance(obs, dict):
            failures += 1
            if failures > 20:
                return 1
            time.sleep(2)
            continue
        failures = 0
        if obs.get("done"):
            return 0
        pending = obs.get("pending") or []
        if not pending:
            time.sleep(1)
            continue
        # One at a time, lowest game first: the same order on every pass.
        pending.sort(key=lambda p: (p.get("game") or 0, p.get("decision") or ""))
        first = pending[0]
        body = answer(first)
        status, _ = env_post("/act", body)
        if status != 200:
            # The game moved on or refused the answer: re-observe and adapt.
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
