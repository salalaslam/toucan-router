"""The done gate: a send that tells a person work is done, fixed, ready or live needs proof.

Proof is a GitHub issue or PR comment, posted after the request arrived (or in the last day for a
plain `toucan send`), that shows a screenshot or output. A message that says the work is not checked
yet passes, and so does a "done" or "ready" for a document or link when the message links to it.

`settings.done_gate` is "off" (the default), "shadow" (record what it would hold, send anyway) or
"enforce" (refuse the send). There is no override: the thread adds proof or says it plainly isn't
checked yet. A failed GitHub read holds the send too, because a check that couldn't run is not a pass.
"""
import json
import re

from .config import Refused, ValidationError, timestamp
from .effects import local_run

PROOF_HOURS = 24

# Claims about software that need a proof comment.
CODE = re.compile(r"(?i)\b(fixed|live|resolved|verified|working now|works now|now works|now working|"
                  r"match(?:es)? the (?:approved )?(?:wireframes?|designs?|mock-?ups?))\b")
# "done" and "ready" need a proof comment when they are about a site or environment, else a link to the thing.
SOFT = re.compile(r"(?i)\b(done|ready|complete|completed|finished)\b")
PLACE = re.compile(r"(?i)\b(dev|test site|staging|prod|production|preview|main site|experimental site|deployed|merged|"
                   r"on (?:the )?site|on test)\b")
# Words just before a claim that make it not a claim: "not fixed", "isn't live yet".
NEGATED = re.compile(r"(?i)\b(not|isn't|aren't|wasn't|weren't|isnt|arent|no|never|nor|yet to be|until|before)\s+(?:\w+\s+){0,2}$")
# A claim later in a sentence that starts with one of these is a plan, not a claim: "once it's live".
FUTURE = re.compile(r"(?i)\b(once|when|whenever|will|i'll|we'll|it'll|after|until|as soon as|if|tell me|let me know|"
                    r"should be|would be|going to|expect|expected|target|aim)\b")
# The honest form the rule asks for.
UNCHECKED = re.compile(r"(?i)\b(not (?:yet )?(?:been )?(?:checked|verified|tested)|"
                       r"(?:haven't|hasn't|have not|has not) (?:yet )?(?:been )?(?:checked|verified|tested))\b")
PROOF = re.compile(r'https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)')
URL = re.compile(r'https?://[^\s<>()\[\]"\']+')
# A screenshot (pasted image or <img>) or output (a fenced block).
EVIDENCE = re.compile(r'(?is)!\[[^\]]*\]\(|<img\b|github\.com/user-attachments/|```')


class Held(Refused):
    """The gate refused a send. Recorded as a done_gate event after the send's transaction rolls back."""
    kind = 'done_gate'

    def __init__(self, message, checks):
        super().__init__(message)
        self.checks, self.thread, self.request = checks, None, None


def claims(text):
    """The claim words in text that are stated as fact, in order. Plans and negations don't count."""
    code, soft = [], []
    for sentence in re.split(r'(?<=[.!?:;\n])\s+', text or ''):
        for pattern, found in ((CODE, code), (SOFT, soft)):
            for match in pattern.finditer(sentence):
                before = sentence[:match.start()]
                if NEGATED.search(before[-40:]) or FUTURE.search(before):
                    continue
                if pattern is SOFT and PLACE.search(sentence):
                    code.append(match.group(0))
                else:
                    found.append(match.group(0))
    return code, soft


def proof_problem(fx, url, repo, number, comment, since):
    """None when the comment is real proof, else what is wrong with it."""
    result = local_run(fx, ['gh', 'api', f'repos/{repo}/issues/comments/{comment}'], timeout=20)
    if result.returncode != 0:
        return f'could not read {url} from GitHub (gh exit {result.returncode}), so it is not proof yet'
    try:
        data = json.loads(result.stdout or '{}')
        posted = timestamp(data['created_at'])
        on = (data.get('issue_url') or '').rstrip('/').rsplit('/', 1)[-1]
    except (ValueError, KeyError, TypeError, ValidationError):
        return f'GitHub returned something unexpected for {url}'
    if on != number:
        return f'{url} is not a comment on #{number}'
    if posted < since:
        return f'{url} was posted before this request, so it does not prove the current work'
    if not EVIDENCE.search(data.get('body') or ''):
        return f'{url} has no screenshot or output in it'
    return None


def check(app, req, text):
    """Reasons to hold this send, or [] when it may go. Called only for sends to people."""
    code, soft = claims(text)
    if not code and not soft:
        return [], None
    if UNCHECKED.search(text or ''):
        return [], dict(claims=code + soft, passed='says not checked yet')
    proofs = PROOF.findall(text or '')
    if not code and not proofs and URL.search(text or ''):
        return [], dict(claims=soft, passed='links to what it delivers')
    if not proofs:
        words = ', '.join(dict.fromkeys(w.lower() for w in code + soft))
        return [f'it says {words} but has no proof. Post a screenshot or output on the GitHub issue, checked on the '
                'environment the person uses against their own example, and link that comment; or say plainly that it '
                'is deployed but not checked yet, and what is left to check'], dict(claims=code + soft)
    since = app.fx.now() - PROOF_HOURS * 3600
    if req is not None:
        row = app.db.execute('SELECT m.ts FROM requests r JOIN messages m ON m.id=r.message_id WHERE r.id=?', (req,)).fetchone()
        if row and row[0]:
            since = row[0]  # proof of this request's work comes after the request
    reasons = []
    for repo, number, comment in proofs:
        url = f'https://github.com/{repo}/issues/{number}#issuecomment-{comment}'
        try:
            problem = proof_problem(app.fx, url, repo, number, comment, since)
        except Exception as error:  # a check that couldn't run is not a pass
            problem = f'checking {url} failed ({type(error).__name__}), so it is not proof yet'
        if problem:
            reasons.append(problem)
    return reasons, dict(claims=code + soft, proofs=[p[2] for p in proofs])


def gate(app, req, text):
    """Run before a send to a person is queued. Returns the check to record; raises Held in enforce mode."""
    mode = app.settings.get('done_gate', 'off')
    if mode == 'off':
        return None
    reasons, detail = check(app, req, text)
    if detail is None:
        return None
    result = dict(detail, mode=mode, verdict='hold' if reasons else 'pass', reasons=reasons)
    if reasons and mode == 'enforce':
        raise Held('Done gate held this send: ' + '; '.join(reasons) + '. There is no override.',
                   [dict(result, refused=True)])
    return result
