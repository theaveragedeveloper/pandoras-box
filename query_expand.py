"""Bounded query expansion for the offline reference library.

Kiwix search is literal keyword matching: it matches the words in the
query, not what the user means. "How to give stitches" matches sewing
articles (the word "stitch" is mostly sewing in Wikipedia) and never
returns the Suture article; "how to filter pond water" matches koi-pond
articles and never returns Water purification. The content is in the
Wikipedia maxi ZIM — the search just never asks for it by the right name.

This module expands a query into additional query variants, so the
Kiwix plug-in asks for the right concept as well as the user's words.
Expansion is ADDITIVE: it only adds candidates, never removes any of
the user's own results. The plug-in merges variants round-robin, so
each variant's top hit stays visible even when the original query
alone would fill every slot with the wrong sense of a word. The
direct-answer scorer also takes the best score across variants, so
"Suture" can win the answer box for "how to give stitches".

Seeded with the survival topics the owner actually searches: medical /
first aid, water, shelter, fire, navigation, radio, food. Add more
rows as new gaps show up — one line each.
"""

import re

# trigger phrase (lowercase) -> expansion queries to also try.
# Longest triggers match first, so "filter pond water" wins over "pond
# water" and "water". A trigger matches on word boundaries, so "burn"
# does not fire inside "burning" unless the whole word is there.
ALIASES = {
    # -- medical / first aid -------------------------------------------
    "stitches": ["suture", "surgical suture"],
    "stitch a wound": ["suture"],
    "give stitches": ["suture"],
    "stitch": ["suture"],
    "cpr": ["cardiopulmonary resuscitation"],
    "bleeding": ["hemorrhage"],
    "stop bleeding": ["hemorrhage", "tourniquet"],
    "broken bone": ["fracture", "bone fracture"],
    "burn": ["burn injury"],
    "wound": ["wound care"],
    "wound care": ["wound healing"],
    "infection": ["wound infection"],
    "splint": ["splint (medicine)"],
    "first aid": ["first aid"],
    "choking": ["choking"],
    "hypothermia": ["hypothermia"],
    "heat stroke": ["heat stroke"],
    "heatstroke": ["heat stroke"],
    "snake bite": ["snakebite"],
    "snakebite": ["snakebite"],
    "sprain": ["sprain"],
    "dislocation": ["joint dislocation"],
    "frostbite": ["frostbite"],
    "dehydration": ["dehydration"],
    "poisoning": ["poisoning"],
    "allergic reaction": ["anaphylaxis"],
    "anaphylaxis": ["anaphylaxis"],
    "seizure": ["seizure"],
    "shock": ["shock (circulatory)"],
    # -- water ----------------------------------------------------------
    "filter water": ["water purification"],
    "pond water": ["water purification"],
    "filter pond water": ["water purification", "portable water purification"],
    "purify water": ["water purification"],
    "clean water": ["water purification"],
    "drinking water": ["water purification"],
    "boil water": ["water purification"],
    "make water safe": ["water purification"],
    "find water": ["water purification"],
    # -- shelter ---------------------------------------------------------
    "build a shelter": ["survival shelter"],
    "shelter": ["survival shelter", "emergency shelter"],
    "lean-to": ["lean-to"],
    # -- fire ------------------------------------------------------------
    "start a fire": ["fire making", "firelighting"],
    "make fire": ["fire making"],
    "light a fire": ["fire making"],
    "fire starting": ["fire making"],
    # -- navigation ------------------------------------------------------
    "navigate": ["navigation"],
    "map reading": ["map reading"],
    "orienteering": ["orienteering"],
    "read a compass": ["compass"],
    "use a compass": ["compass"],
    # -- radio -----------------------------------------------------------
    "ham radio": ["amateur radio"],
    "repeater": ["amateur radio repeater"],
    # -- food ------------------------------------------------------------
    "forage": ["foraging"],
    "edible plants": ["edible plants"],
    "find food": ["foraging"],
    # -- general survival -------------------------------------------------
    "survival": ["survival skills"],
    "knots": ["knot"],
    "tie a knot": ["knot"],
    # -- household and vehicle manuals ---------------------------------
    "keep pipes from freezing": ["winterize pipes", "pipe insulation"],
    "pipes from freezing": ["winterize pipes", "pipe insulation"],
    "frozen pipes": ["pipe winterization"],
    "winterisation": ["winterization"],
    "ups": ["uninterruptible power supply"],
    "tyre pressure": ["tire pressure"],
    "vehicle won't start": ["starting system troubleshooting"],
    "carbon monoxide alarm": ["carbon monoxide detector"],
    # Common misspellings. Corrections are always extra search terms.
    "winterizaton": ["winterization"],
    "winteriztion": ["winterization"],
    "insluation": ["insulation"],
    "tourniqet": ["tourniquet"],
    "water purfication": ["water purification"],
    "orienteereing": ["orienteering"],
}

# Total variants per search, including the original query (so at most
# MAX_VARIANTS - 1 expansions). Each variant is one extra kiwix-serve
# request, so keep it small.
MAX_VARIANTS = 3


_TRIGGERS = [
    (re.compile(r"\b" + re.escape(trigger) + r"\b"), ALIASES[trigger])
    for trigger in sorted(ALIASES, key=len, reverse=True)
]


def search_words(text):
    """Words used for case- and punctuation-insensitive relevance checks."""
    return re.findall(r"[^\W_]+", text.casefold())


def expand_query(query):
    """Return [original query, ...expansion queries] for a user query.

    The original query is always first and always included. Expansions
    come from ALIASES whose trigger phrase appears in the query
    (longest trigger first, so the most specific concept wins). The
    list is capped at MAX_VARIANTS so a search stays cheap.
    """
    if not query or not query.strip():
        return [query]
    normalized = query.casefold()
    variants = [query]
    seen = {normalized}
    # Longest trigger first: "filter pond water" before "pond water"
    # before "water", so the specific expansion is tried first.
    for trigger, targets in _TRIGGERS:
        if trigger.search(normalized):
            for target in targets:
                if target.lower() not in seen:
                    seen.add(target.lower())
                    variants.append(target)
                if len(variants) >= MAX_VARIANTS:
                    return variants
    return variants
