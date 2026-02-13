"""
Word list for SDLC approval token generation.

Contains ~200 concrete, unambiguous, easy-to-spell English nouns (3-7 chars each).
3-word tokens yield 200^3 = 8M combinations, sufficient for ephemeral single-session use.
"""

WORD_LIST = [
    # Animals
    "BEAR", "BIRD", "CAT", "CRAB", "CROW", "DEER", "DOG", "DOVE", "DUCK",
    "EAGLE", "EEL", "ELK", "FISH", "FOX", "FROG", "GOAT", "GOOSE", "HAWK",
    "HARE", "HORSE", "LAMB", "LION", "MOLE", "MOOSE", "MOTH", "MOUSE",
    "NEWT", "OWL", "PANDA", "PIG", "PONY", "RAM", "ROBIN", "SEAL", "SHARK",
    "SHEEP", "SLUG", "SNAIL", "SNAKE", "STORK", "SWAN", "TIGER", "TOAD",
    "TROUT", "VIPER", "WASP", "WHALE", "WOLF", "WORM", "ZEBRA",
    # Colors
    "AMBER", "BLACK", "BLUE", "BROWN", "CORAL", "CREAM", "GOLD", "GREEN",
    "GREY", "IVORY", "LILAC", "NAVY", "OLIVE", "PEACH", "PINK", "PLUM",
    "RED", "RUBY", "RUST", "TAN", "TEAL", "WHITE",
    # Fruits & food
    "APPLE", "BASIL", "BEAN", "BERRY", "BREAD", "CANDY", "CEDAR", "CHERRY",
    "CHILI", "COCOA", "DATE", "FIG", "GRAPE", "GUAVA", "HONEY", "KIWI",
    "LEMON", "LIME", "MANGO", "MAPLE", "MELON", "MINT", "OLIVE", "PEACH",
    "PEAR", "PLUM", "RICE", "SAGE", "WHEAT",
    # Weather & sky
    "BOLT", "BREEZE", "CLOUD", "DEW", "FLAME", "FLOOD", "FOG", "FROST",
    "GALE", "HAIL", "ICE", "MIST", "MOON", "RAIN", "SKY", "SLEET",
    "SNOW", "STAR", "STORM", "SUN", "TIDE", "WIND",
    # Nature & landscape
    "ASH", "BAY", "BLUFF", "BOG", "BROOK", "CAPE", "CAVE", "CLIFF",
    "COAST", "COVE", "CREEK", "DALE", "DELTA", "DUNE", "FALLS", "FIELD",
    "FJORD", "GLEN", "GROVE", "GULF", "HILL", "ISLE", "LAKE", "MARSH",
    "MESA", "MOSS", "OAK", "PALM", "PEAK", "PINE", "POND", "REEF",
    "RIDGE", "RIVER", "ROCK", "SAND", "SHORE", "SLOPE", "STONE", "TRAIL",
    "VALE", "VINE", "WOOD",
    # Tools & objects
    "ANVIL", "AXE", "BELL", "BOLT", "BOW", "BRICK", "BRUSH", "CHAIN",
    "CHEST", "CLOCK", "COIN", "CROWN", "DRUM", "FLUTE", "FORGE", "GATE",
    "GEAR", "GLASS", "GLOBE", "GONG", "HARP", "HELM", "HORN", "KEY",
    "KNOT", "LANCE", "LATCH", "LENS", "LOCK", "MASK", "NAIL", "OPAL",
    "PEARL", "PIKE", "PIPE", "PRISM", "RING", "ROPE", "SCALE", "SHIELD",
    "SPEAR", "SPIKE", "SWORD", "TORCH", "TOWER", "VAULT", "WAGON",
    "WHEEL",
]

# Deduplicate while preserving order
_seen: set[str] = set()
_unique: list[str] = []
for _w in WORD_LIST:
    if _w not in _seen:
        _seen.add(_w)
        _unique.append(_w)
WORD_LIST = _unique
del _seen, _unique, _w
