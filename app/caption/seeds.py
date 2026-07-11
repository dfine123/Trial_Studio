"""THE SEED BANK — variation seeds for v3 generation.

A seed is NOT a topic. It's mechanical entropy: a subject drawn at true random and handed to
every generation engine to knock its brain off the default path. The caption owes the seed
NOTHING — most captions shouldn't even mention it (the operator: "i cant emphasize enough how
little the end caption has to do with the actual outputs, its just a variation seed").

Drawn with random.choice — never an LLM (an LLM asked for "random" converges; dice don't).
The bank spans in-world subjects, everyday life, and fully abstract territory on purpose:
close seeds pull specifics, far seeds force associations nobody would default to.
"""
from __future__ import annotations

import random

BANK = [
    # ── his world, adjacent ──
    "crypto losses", "a maxed-out credit card", "the casino parking lot", "a pawn shop",
    "payday", "rent day", "a job interview", "a group project", "LinkedIn", "a resume",
    "quitting", "a two-week notice", "the stock market open", "a trading app notification",
    "a sports bar on sunday", "a scratch ticket", "valet parking", "a rooftop bar",
    "bottle service", "a business card", "a pitch meeting", "an invoice", "a refund request",
    "the group chat", "a missed call from mom", "a wedding toast", "a bachelor party",
    "a car dealership", "a test drive", "an oil change", "a parking ticket", "a speeding ticket",
    "jury duty", "a tax refund", "an audit", "a landlord", "a security deposit",
    "a roommate", "a lease renewal", "student loans", "graduation", "a class reunion",
    "a high school teacher", "a guidance counselor", "an internship", "a performance review",
    "employee of the month", "a name tag", "a uniform", "a lanyard", "a cubicle",
    "a conference call", "a team-building retreat", "a suggestion box", "overtime",
    "a lunch break", "the office fridge", "a retirement party", "a pension", "a 401k",
    "day one of the gym", "a personal trainer", "protein powder", "a gym mirror",
    "new year's resolutions", "a vision board", "a podcast microphone", "a webinar",
    "an online course", "a mastermind group", "a mentor", "a guru", "a seminar",
    "airport first class", "a boarding pass", "a layover", "baggage claim", "TSA",
    "a hotel minibar", "room service", "a cruise ship", "an all-inclusive resort",
    "a timeshare presentation", "a yacht club", "a country club", "a golf course",
    "a watch store", "a jeweler", "a barber shop", "a fresh fade", "designer shoes",
    "an outlet mall", "a receipt", "a price tag", "a warranty", "an extended warranty",
    "a gift card", "a loyalty program", "cash back", "a coupon", "a clearance rack",
    # ── everyday life ──
    "mattress shopping", "assembling furniture", "a moving truck", "cardboard boxes",
    "a storage unit", "a garage sale", "a lost remote", "a dead phone battery",
    "a cracked screen", "phone storage full", "an update pending", "airplane mode",
    "a wifi password", "a hotspot", "a printer", "a scanner", "a fax machine",
    "leftovers", "meal prep", "a grocery list", "self checkout", "a shopping cart",
    "expired milk", "a microwave dinner", "a smoke alarm", "a toaster", "an air fryer",
    "a slow cooker", "a food truck", "a drive-thru", "a value menu", "a kids menu",
    "a birthday cake", "a candle", "a pinata", "a bounce house", "a magician",
    "a school play", "a talent show", "a science fair", "a spelling bee", "recess",
    "a substitute teacher", "a permission slip", "a field trip", "a yearbook",
    "a driving test", "parallel parking", "a road trip", "a gas station", "a rest stop",
    "a toll booth", "cruise control", "a check engine light", "a spare tire",
    "jumper cables", "a car wash", "an air freshener", "a sunroof", "a horn",
    "a doorbell camera", "a neighborhood watch", "an HOA", "a lawnmower", "a leaf blower",
    "a garden gnome", "a sprinkler", "a garage door", "a welcome mat", "a mailbox",
    "a package delivery", "porch pirates", "a missed delivery slip", "return labels",
    "a barbecue", "a cooler", "a lawn chair", "a tailgate", "a bonfire", "fireworks",
    "a fishing trip", "a camping tent", "a sleeping bag", "bug spray", "a flashlight",
    "a power outage", "a generator", "a snow day", "a heat wave", "an umbrella",
    "a weather app", "a pollen count", "allergy season", "a sneeze", "hiccups",
    "a dentist appointment", "a waiting room", "a clipboard", "a copay", "insurance",
    "an eye exam", "reading glasses", "a prescription", "a pharmacy line", "vitamins",
    "an ice bath", "a sauna", "a massage chair", "a chiropractor", "a foam roller",
    "a haircut gone wrong", "a cowlick", "a receding hairline", "a beard trimmer",
    "a mirror selfie", "a passport photo", "a driver's license photo", "an ID check",
    # ── animals & nature ──
    "raccoons", "pigeons", "seagulls", "squirrels", "a stray cat", "a guard dog",
    "a goldfish", "an aquarium", "a parrot", "a peacock", "a rooster", "an owl",
    "a shark", "a dolphin", "an octopus", "a jellyfish", "a lobster tank", "a crab",
    "ants", "a beehive", "a mosquito", "a spider in the corner", "a moth", "fireflies",
    "a lion", "a silverback gorilla", "a cheetah", "a sloth", "a camel", "a llama",
    "a horse race", "a petting zoo", "a bird feeder", "a squirrel on a wire",
    "a full moon", "a solar eclipse", "a shooting star", "a thunderstorm", "fog",
    "a desert", "an iceberg", "a volcano", "quicksand", "a rip current", "a cliff edge",
    # ── internet & culture ──
    "video games", "a loading screen", "a lag spike", "a respawn timer", "a final boss",
    "a speedrun", "a cheat code", "a battle pass", "loot boxes", "a high score",
    "an arcade", "a claw machine", "skee ball", "a prize counter", "tickets",
    "a streamer", "a donation alert", "a subscriber count", "going live", "a VOD",
    "a comment section", "a ratio", "a repost", "a story view", "read receipts",
    "a typing bubble", "a voice memo", "a group facetime", "a spam call", "voicemail",
    "a dating app bio", "a first date", "a situationship", "a wingman", "a rizz",
    "a gender reveal", "a proposal video", "a wedding hashtag", "a honeymoon fund",
    "a family group chat", "a baby monitor", "a minivan", "a car seat", "a stroller",
    "a fantasy league", "a draft night", "a parlay", "a buzzer beater", "overtime",
    "a referee", "a mascot", "a halftime show", "a jumbotron", "season tickets",
    "a boxing weigh-in", "a press conference", "a post-game interview", "a trophy",
    "a participation medal", "a hall of fame", "a retired jersey", "a rookie card",
    "monopoly", "poker night", "a chess clock", "a rubik's cube", "a crossword",
    "a magic 8 ball", "a fortune cookie", "a horoscope", "a tarot card", "a psychic",
    "a wishing well", "a lucky penny", "a rabbit's foot", "a four leaf clover",
    # ── abstract / far ──
    "submarines", "the roman empire", "a medieval castle", "a drawbridge", "a moat",
    "a knight's armor", "a catapult", "a treasure map", "a pirate ship", "a compass",
    "a lighthouse", "a message in a bottle", "a desert island", "a coconut",
    "outer space", "an astronaut", "a space station", "zero gravity", "an alien",
    "a UFO sighting", "a black hole", "a telescope", "a meteor shower", "mars",
    "a time machine", "a parallel universe", "deja vu", "a dream journal", "sleep paralysis",
    "an escape room", "a haunted house", "a ouija board", "a ghost tour", "a seance",
    "a museum", "a dinosaur skeleton", "a mummy", "an ancient scroll", "hieroglyphics",
    "a library card", "a due date", "a librarian", "a bookmark", "an audiobook",
    "a vending machine", "an elevator", "an escalator", "a revolving door", "a fire exit",
    "a fire drill", "a security camera", "a metal detector", "a velvet rope", "a bouncer",
    "a karaoke machine", "a jukebox", "a disco ball", "a fog machine", "a dj booth",
    "an open mic", "a stand-up special", "a laugh track", "a teleprompter", "cue cards",
    "a red carpet", "an award show", "an acceptance speech", "a paparazzi photo",
    "a wax museum", "a lookalike contest", "a talent agent", "an audition", "a callback",
    "a marathon", "a finish line", "a water station", "a medal ceremony", "a photo finish",
    "a triathlon", "a relay baton", "a starting gun", "a false start", "a personal record",
    "a mountain summit", "a base camp", "an avalanche", "a ski lift", "a black diamond",
    "a scuba tank", "a coral reef", "a shipwreck", "buried treasure", "a metal detector on a beach",
    "a sandcastle", "a tide pool", "a boardwalk", "a ferris wheel", "a roller coaster",
    "a haunted carnival", "cotton candy", "a funnel cake", "a corn maze", "a hayride",
    "a pumpkin patch", "a snow globe", "an advent calendar", "a white elephant gift",
    "a secret santa", "a new year's kiss", "a resolution list", "a countdown clock",
    "a leap year", "daylight savings", "a sundial", "an hourglass", "a grandfather clock",
    "a cuckoo clock", "a stopwatch", "an alarm clock", "a snooze button", "jet lag",
    # ── people & tensions (the winners' world is people, not products — 2026-07-10) ──
    "a bro's new girlfriend", "the quiet guy in the group chat", "a hater from high school",
    "her dad", "a cousin with a scheme", "the friend who never has cash", "a wingman",
    "the guy who peaked in high school", "a former teacher seeing you now", "the family skeptic",
    "a rich uncle", "a broke landlord", "the neighbor with the boat", "a trust fund kid",
    "the friend with a podcast", "a girl's best friend", "the ex who doubted you",
    "the boys' annual trip", "a bro going through it", "the one married friend",
    "the friend who's always 'about to launch'", "a gym bro", "the coworker who snitches",
    "an old boss", "the guy still asking for the homework", "a girlfriend's mother",
    "the friend who found a guru", "a bro's terrible investment", "the group chat economist",
    "the guy who knows a guy", "a barber's opinions", "an uber driver's life story",
    "the youngest guy at the table", "the oldest intern", "a nephew asking questions",
    "the friend who moved away", "a bro's wedding", "the one who made it out",
    "a doubter at thanksgiving", "the friend who counts your drinks", "a valet's judgment",
    "the girl from the DMs", "a waiter watching the bill land", "the plug's business hours",
    "a pastor's side hustle", "the loudest guy at the casino", "a bouncer's memory",
    "the accountant's sigh", "a loan officer's face", "the dealership guy's handshake",
]
BANK = list(dict.fromkeys(BANK))   # dedupe, order-preserving


def draw() -> str:
    """One seed, true random. ~1 in 8 draws collides two seeds ("X + Y") for extra entropy."""
    if random.random() < 0.125:
        a, b = random.sample(BANK, 2)
        return f"{a} + {b}"
    return random.choice(BANK)
