"""Decision cards: the short, fixed-shape WhatsApp message an escalation becomes.

An owner thread can't send the owner free text. It names a category from CATEGORIES, one
question and two to four options with its pick, and Toucan renders the card. The thread's
longer note is kept and sent only when the owner replies "more". Cards are numbered by request,
so "T435 B" answers request 435 with option B."""
import json
import re

from .config import Refused

# What only the owner can decide. Anything else the thread settles itself or asks the requester.
CATEGORIES = {
    'deploy': 'a production deploy, release or merge',
    'client': 'a commitment to a client: dates, prices, promises or anything sent in the owner\'s name',
    'scope': 'scope, an estimate or priorities between projects',
    'money': 'spending money',
    'access': 'access or credentials',
    'asked': 'the requester asked for the owner personally',
}
ROUTING = 'routing'  # Toucan's own: no thread would take the request
LETTERS = 'ABCD'
LIMITS = dict(want=120, question=160, option=80)
CARD_ID = re.compile(r'^\s*\*?T(\d+)\*?(?=$|[\s:.,;)\-])[\s:.,;)\-]*(.*)$', re.I | re.S)
MORE = re.compile(r'^\s*more(?:\s+\*?T?(\d+)\*?)?\s*[.!?]?\s*$', re.I)
# A letter alone, or followed by punctuation: "B", "b.", "B) but tomorrow", "B, and tell her". "a bit later" is a sentence.
LETTER = re.compile(r'^\s*\(?([A-D])(?:\)?\s*$|\)?\s*[:.,;)\-]\s*(.*)$)', re.I | re.S)
DETAIL_MAX = 3500

BAR = ('Escalate only what the owner alone can decide: ' + '; '.join(f'{k} ({v})' for k, v in CATEGORIES.items()) +
       '. Settle anything else yourself or ask the requester.')
USAGE = ('toucan escalate REQ --claim ID --category CATEGORY --want "what they want, one line" '
         '--question "the one thing to decide" --option "first choice" --option "second choice" [--option ...] '
         '--pick A --note "full context, shown only if the owner replies more"')


def one_line(value):
    return ' '.join((value or '').split())


def card_id(request_id):
    return f'T{request_id}'


def validate(category, want, question, options, pick):
    """The hard gate. Returns (want, question, options, pick index) or raises Refused."""
    problems = []
    if category not in CATEGORIES:
        problems.append(f'--category must be one of {", ".join(CATEGORIES)}')
    want, question = one_line(want), one_line(question)
    options = [one_line(o) for o in options or []]
    if not want:
        problems.append('--want is required: what the requester wants, in one line')
    elif len(want) > LIMITS['want']:
        problems.append(f'--want is {len(want)} characters; keep it under {LIMITS["want"]}')
    if not question:
        problems.append('--question is required: the one decision you need')
    elif len(question) > LIMITS['question']:
        problems.append(f'--question is {len(question)} characters; keep it under {LIMITS["question"]}, and ask one thing')
    if not 2 <= len(options) <= len(LETTERS):
        problems.append(f'give 2 to {len(LETTERS)} --option choices, not {len(options)}')
    for i, option in enumerate(options):
        if not option or len(option) > LIMITS['option']:
            problems.append(f'option {LETTERS[i] if i < len(LETTERS) else i + 1} must be 1 to {LIMITS["option"]} characters')
    index = LETTERS.find((pick or '').strip().upper()) if len((pick or '').strip()) == 1 else -1
    if index < 0 or index >= len(options):
        problems.append('--pick must be the letter of your suggested option (A, B, ...)')
    if problems:
        raise Refused('Escalation refused: ' + '; '.join(problems) + f'.\n{BAR}\nUsage: {USAGE}')
    return want, question, options, index


def render(request_id, thread, name, want, question, options, pick, replaces=False):
    lines = [f'*{card_id(request_id)}* · {thread} · {name}']
    if replaces:
        lines.append(f'New question; replaces the earlier {card_id(request_id)} card.')
    lines += [f'Wants: {want}', f'Decide: {question}']
    for i, option in enumerate(options):
        lines.append(f'{LETTERS[i]}) {option}' + ('  ← suggested' if i == pick else ''))
    answer = f'{card_id(request_id)} {LETTERS[pick]}' if options and pick is not None else f'{card_id(request_id)} your answer'
    lines.append(f'Reply "{answer}", quote this, or "more"')
    return '\n'.join(lines)


def compact(card, thread, name, since):
    """One card as an entry in the daily list."""
    options = json.loads(card['options_json'] or '[]')
    lines = [f'*{card_id(card["request_id"])}* {thread} · {name} (since {since})', card['question']]
    if options:
        lines.append('  '.join(f'{LETTERS[i]}) {o}' + (' ←' if i == card['pick'] else '') for i, o in enumerate(options)))
    return '\n'.join(lines)


def detail(card, thread, name, asked):
    lines = [f'*{card_id(card["request_id"])}* detail · {thread}', f'{name} asked:', (asked or '').strip()[:1500], '',
             'Note from the thread:', (card['detail'] or 'none').strip()]
    for update in json.loads(card['updates_json'] or '[]'):
        lines += ['', f'Update {update["at"]}:', update['note'].strip()]
    text = '\n'.join(lines)
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX - 1] + '…'


def expand(text, options):
    """'B' or 'b, but tomorrow' answers with option B spelled out, so the thread reads the choice."""
    match = LETTER.match(text or '')
    if not options or not match or LETTERS.index(match[1].upper()) >= len(options):
        return text
    letter = match[1].upper()
    rest = (match[2] or '').strip()
    chosen = f'{letter}) {options[LETTERS.index(letter)]}'
    return f'{chosen}. {rest}' if rest else chosen


def same_question(a, b):
    norm = lambda s: re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()
    return norm(a) == norm(b)
