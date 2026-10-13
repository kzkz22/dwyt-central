"""When YouTube captions are good enough (decision 18.9).

YouTube's automatic captions are often weak (Hungarian, technical talk) and never tell speakers
apart. The user decides once: only the uploader's own captions (default), machine captions too, or
always a real transcription; with a list of languages where machine captions are fine anyway
("en"). A channel may override the rule, and a single submission may too.

The rule travels with a captions job (`payload.captions_policy`, not in the options: the options
make the shared-result recipe). The runner downloads whichever track it finds; the central checks
the result: a machine track the rule does not accept is not stored, and the job moves on in its
chain (subscription videos: local Whisper, then cloud if allowed) or ends with a clear message
(a single submission: no silent switch to another mode). Shared results are taken over only when
the rule accepts them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import db
from .models import Subscription, User

POLICIES = {
    "manual": "Csak a feltöltő saját felirata",
    "any": "Gépi felirat is jó",
    "never": "Mindig átírás",
}
HINTS = {
    "manual": "Ha a videó készítője maga töltött fel feliratot, azt használjuk (pontos, ingyenes, gyors). Ha csak "
              "gépi felirat van, átírás készül.",
    "any": "A leggyorsabb és ingyenes, de a gépi felirat pontatlanabb lehet, és nincsenek benne beszélők.",
    "never": "Mindig beszélőkkel átírás készül, felirat nélkül. A legjobb minőség; felhőben ez a legdrágább.",
}
DEFAULT = "manual"


@dataclass(frozen=True)
class Policy:
    mode: str = DEFAULT            # manual | any | never
    auto_langs: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {"mode": self.mode, "auto_langs": list(self.auto_langs)}

    @classmethod
    def from_payload(cls, data: Any) -> Policy | None:
        if not isinstance(data, dict) or data.get("mode") not in POLICIES:
            return None
        return cls(data["mode"], tuple(str(x) for x in data.get("auto_langs") or [] if isinstance(x, str)))


def parse_langs(raw: str) -> tuple[str, ...]:
    """'en, DE; pt-BR' -> ('en', 'de', 'pt'): base language codes, at most 20."""
    out: list[str] = []
    for code in re.split(r"[\s,;]+", (raw or "").lower()):
        base = code.split("-")[0]
        if re.fullmatch(r"[a-z]{2,3}", base) and base not in out:
            out.append(base)
    return tuple(out[:20])


def for_user(user_id: int, sub: Subscription | None = None, override: str | None = None) -> Policy:
    """The rule that applies: a submission's choice, else the channel's, else the user's."""
    with db.system_session() as s:
        user = s.get(User, user_id)
        mode = user.captions_policy if user is not None and user.captions_policy in POLICIES else DEFAULT
        langs = parse_langs(user.captions_auto_langs if user is not None else "")
    if sub is not None and sub.captions_policy in POLICIES:
        mode = sub.captions_policy
    if override in POLICIES:
        mode = override
    return Policy(mode, langs)


def accepts(policy: Policy | None, caption_kind: str | None, language: str | None) -> bool:
    """Does this rule accept a captions transcript of this kind (manual | auto) and language?"""
    if policy is None or caption_kind != "auto":
        return True  # the uploader's own captions, or a job from before the rule existed
    if policy.mode == "any":
        return True
    return (language or "").lower().split("-")[0] in policy.auto_langs


def rejected_message(language: str | None, subscription: bool) -> str:
    lang = f" ({language})" if language else ""
    if subscription:
        return f"Csak gépi felirata van{lang}; a beállításod szerint ezt nem használjuk."
    return (f"Csak gépi felirata van{lang}, és a beállításod szerint ezt nem használjuk. Kérj helyi vagy felhős "
            "átírást az elem oldalán, vagy a Beállításokban engedd meg a gépi feliratot.")
