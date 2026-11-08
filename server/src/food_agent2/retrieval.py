"""``find_food``: the one retrieval ladder. Every rung resolves, or the next one tries.

Order, by the item's kind (checked in code, the model's kind is only a hint):

1. Exact: saved usuals, the tenant catalog, curated rows, each lane's exact match.
2. Whole foods: the food without its preparation words (counted foods only), a
   typical dish built from pinned rows, then the closest USDA row.
   Named products: the closest FatSecret hit under the same brand.
3. A labeled web estimate. This one is slow, so the caller runs it apart from the
   rest (:meth:`Retrieval.find_on_web`) and it never holds up a ready item.
4. Not counted, with a reason.

Hard guards (:mod:`food_agent2.match`) apply on every rung. Anything else that
differs from what the user said is disclosed as "matched to X".

One :class:`Scope` lives for one turn. It memoizes every provider call and enforces
a per-turn cap, so a whole meal stays within budget.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import dish_estimates
import food_lookup
import food_words
import usda_lookup
from food_agent import portion as portion_math
from food_agent.contracts import DraftItemState, FoodIdentity, NutrientPanel, Portion, ResolvedFood
from food_agent.find_food import identity_query, to_resolved_food, with_serving_unit
from food_agent2 import match, menu_pick
from food_agent2.sources import Sources

logger = logging.getLogger(__name__)

MAX_CALLS_PER_TURN = 8
MAX_WEB_PER_TURN = 2
_MAX_FETCHES = 2
# A word score this high is the item as named: no inference needed.
CLEAR_MATCH = 0.9
_FORM_PENALTY = 0.03
_TRAILING_CATEGORY = re.compile(r"\s+(?:sandwich|meal|combo|entree|dish|item)$", re.IGNORECASE)

Status = Literal["verified", "estimated", "needs_web", "not_counted"]


@dataclass(frozen=True)
class Found:
    status: Status
    food: ResolvedFood | None = None
    note: str | None = None
    reason: str | None = None


class Scope:
    """One turn's provider calls: each distinct call once, and no more than the cap."""

    def __init__(self, max_calls: int, max_web: int):
        self.max_calls, self.max_web = max_calls, max_web
        self.used, self.web_used = 0, 0
        self.memo: dict[tuple[Any, ...], Any] = {}

    async def call(self, key: tuple[Any, ...], run: Callable[[], Awaitable[Any]], *, counted: bool = True,
                   default: Any = None) -> Any:
        if key in self.memo:
            return self.memo[key]
        is_web = key[0] == "web"
        if counted and (self.used >= self.max_calls or (is_web and self.web_used >= self.max_web)):
            return default
        if counted:
            self.used += 1
            self.web_used += 1 if is_web else 0
        try:
            result = await run()
        except Exception as exc:  # noqa: BLE001 - a provider failure is a miss, never a crash
            logger.info("food retrieval: %s failed: %s", key[0], type(exc).__name__)
            result = default
        self.memo[key] = result
        return result


def lane_of(identity: FoodIdentity, query: str) -> str:
    """The source lane. A brand or chain name makes it branded whatever the model said."""
    if identity.brand and identity.kind not in food_lookup.BRANDED_KINDS:
        return "packaged"
    kind = identity.kind if identity.kind in (*food_lookup.BRANDED_KINDS, *food_lookup.WHOLE_FOOD_KINDS) else "generic"
    return food_lookup.route_kind(query, kind) or "generic"


def user_words(identity: FoodIdentity) -> str:
    """What the user called it, for the logged name of a generic food."""
    text = identity_query(identity).strip()
    return text[:1].upper() + text[1:] if text else identity.name


def web_query(identity: FoodIdentity) -> str:
    """The web source must repeat every query word, and pages rarely say "sandwich" after the name.
    Sizes, diameters and amounts are said the way pages say them (food_words)."""
    return _TRAILING_CATEGORY.sub("", food_words.normalize(identity_query(identity)))


def _generic(food: ResolvedFood, identity: FoodIdentity, status: Status) -> Found:
    """A whole food keeps the user's own words. The row it came from is disclosed."""
    words = user_words(identity)
    note = f"matched to {food.display_name}" if match.differs(words, food.display_name) else None
    named = food.model_copy(update={"display_name": words[:200],
                                    "attribution": {**food.attribution, "matched_name": food.display_name}})
    return Found(status, named, note)


def _branded(food: ResolvedFood, identity: FoodIdentity, status: Status) -> Found:
    asked = " ".join(part for part in (identity.brand, match.asked_text(identity)) if part)
    return Found(status, food, f"matched to {food.display_name}" if match.differs(asked, food.display_name) else None)


def _typical(identity: FoodIdentity) -> ResolvedFood | None:
    """A typical dish or staple built from pinned USDA rows. No network, always labeled."""
    built = dish_estimates.estimate({"description": identity_query(identity), "quantity": None, "unit": None})
    if not built or not built.get("built_from"):
        return None
    grams = sum(float(part["grams"]) for part in built["built_from"])
    noun = dish_estimates.dish_name(identity_query(identity)) or identity.name.casefold()
    panel = NutrientPanel(**{key: float(built[key]) for key in ("calories", "protein", "carbs", "fat", "fiber")})
    digest = hashlib.sha256(identity_query(identity).casefold().encode()).hexdigest()[:24]
    return ResolvedFood(
        food_id=f"typical:{digest}", provider="USDA SR Legacy", display_name=str(built["name"])[:200],
        macro_source=str(built["macro_source"])[:200], serving_basis=portion_math.parse_serving_text(f"1 {noun} ({grams:g} g)"),
        panel=panel, attribution={"verification_state": "typical_serving_estimate"})


def named_unit(found: Mapping[str, Any], portion: Portion) -> Mapping[str, Any]:
    """A curated row measured in the unit the user named ("1 tbsp", "2 slices"), from the row's own
    per-unit weights, so the amount counts instead of becoming a question. Otherwise unchanged."""
    weights, per_100g = found.get("unit_grams"), found.get("macros_per_100g")
    if portion.kind == "mass" or not isinstance(weights, Mapping) or not isinstance(per_100g, Mapping):
        return found
    if portion.kind == "volume":
        unit = portion.unit if portion.unit in weights else None
    else:
        unit = next((word for word in re.findall(r"[a-z]+", portion.text.casefold()) if word in weights), None)
    if unit is None:
        return found
    if unit.endswith("s") and unit[:-1] in weights:
        unit = unit[:-1]
    grams = float(weights[unit])
    if not grams > 0:
        return found
    return {**found, "serving_size": f"1 {unit} ({grams:g} g)",
            "macros_per_serving": {key: None if value is None else round(float(value) * grams / 100, 2)
                                   for key, value in per_100g.items()}}


def _row_complete(row: Mapping[str, Any]) -> bool:
    nutrients = usda_lookup._nutrients(dict(row))
    return nutrients["calories"] is not None and not ((nutrients["carbs"] or 0) > 0 and nutrients["fiber"] is None)


def _pick_row(identity: FoodIdentity, wanted: str, rows: Any) -> Mapping[str, Any] | None:
    """The closest row that is not a different food. A complete panel beats a closer word match."""
    asked = match.asked_text(identity)
    best: tuple[tuple[int, float], Mapping[str, Any]] | None = None
    for row in rows or []:
        if not isinstance(row, Mapping) or usda_lookup._nutrients(dict(row))["calories"] is None:
            continue
        description = str(row.get("description") or "")
        if match.conflict(asked, description) or match.cook_conflict(asked, description):
            continue
        if not match.head_present(wanted, description):
            continue
        extra = match.tokens(description) - match.tokens(wanted)
        # USDA descriptions are wordy by nature, so only added forms ("frozen", "breaded") count against a row.
        score = match.closeness(wanted, description, extra_penalty=0.0) \
            - _FORM_PENALTY * len(extra & usda_lookup._FORM_WORDS)
        if score < match.MIN_CLOSENESS - _FORM_PENALTY:
            continue
        rank = (int(_row_complete(row)), score)
        if best is None or rank > best[0]:
            best = (rank, row)
    return best[1] if best else None


class Retrieval:
    def __init__(self, sources: Sources, *, max_calls: int = MAX_CALLS_PER_TURN, max_web: int = MAX_WEB_PER_TURN,
                 picker: Callable[..., Awaitable[menu_pick.Pick]] | None = None):
        self.sources = sources
        self.picker = picker
        self.max_calls, self.max_web = max_calls, max_web

    def scope(self) -> Scope:
        return Scope(self.max_calls, self.max_web)

    # ------------------------------------------------------------ the ladder

    async def find(self, identity: FoodIdentity, portion: Portion, scope: Scope) -> Found:
        """Everything but the web. ``needs_web`` means the caller should run :meth:`find_on_web`."""
        query = identity_query(identity)
        lane = lane_of(identity, query)
        whole = lane in food_lookup.WHOLE_FOOD_KINDS
        hit = await self._exact(identity, portion, query, lane, whole, scope)
        if hit is not None:
            return hit
        if whole:
            if portion.kind != "mass":
                stripped = match.without_preparation(query)
                if stripped != query:
                    hit = await self._exact(identity, portion, stripped, lane, whole, scope)
                    if hit is not None:
                        return hit
            typical = _typical(identity)
            if typical is not None:
                return _generic(typical, identity, "estimated")
            hit = await self._closest_row(identity, portion, query, scope)
        else:
            hit = await self._closest_product(identity, portion, query, scope)
        return hit or Found("needs_web")

    async def find_on_web(self, identity: FoodIdentity, portion: Portion, scope: Scope) -> Found:
        query = web_query(identity)
        found = await scope.call(("web", query.casefold()), lambda: self.sources.web(query))
        food = to_resolved_food(found) if isinstance(found, Mapping) else None
        food = with_serving_unit(food, identity_query(identity), portion)
        if food is None:
            return Found("not_counted", reason="no source had it")
        return Found("estimated", food)

    async def _exact(self, identity, portion, query, lane, whole, scope) -> Found | None:
        found = await scope.call(("exact", query.casefold(), lane), lambda: self.sources.exact(query, lane),
                                 counted=not whole)
        if not isinstance(found, Mapping):
            return None
        food = self._food(found, query, portion)
        if food is None:
            return None
        name = str(found.get("name") or "")
        asked = match.asked_text(identity)
        if match.conflict(asked, name) or (portion.kind == "mass" and match.cook_conflict(asked, found_text(found))):
            return None
        status: Status = "estimated" if "estimate" in food.macro_source.casefold() else "verified"
        return _generic(food, identity, status) if whole else _branded(food, identity, status)

    @staticmethod
    def _food(found: Mapping[str, Any], query: str, portion: Portion) -> ResolvedFood | None:
        if str((found.get("attribution") or {}).get("provider")) == "USDA FDC":
            found = usda_lookup.for_portion(dict(found), query, portion.text, portion.kind, portion.unit)
        else:
            found = named_unit(found, portion)
        return with_serving_unit(to_resolved_food(found), query, portion)

    async def _closest_row(self, identity, portion, query, scope) -> Found | None:
        attempts = [(query, True)]
        head = match.staple_head(identity.name)
        if head and head != query.casefold():
            attempts.append((head, True))
        for text, require_all in attempts:
            rows = await scope.call(("rows", text.casefold(), require_all),
                                    lambda text=text, require_all=require_all: self.sources.rows(text, require_all),
                                    default=[])
            row = _pick_row(identity, text, rows)
            if row is None:
                continue
            found = usda_lookup.to_result(dict(row))
            food = self._food(found, query, portion)
            if food is not None:
                return _generic(food, identity, "verified")
        return None

    async def _closest_product(self, identity, portion, query, scope) -> Found | None:
        """The product the user named, under the same brand.

        Code searches (the phrase as said, then simpler words) and keeps only candidates that
        are not a different food. A clear word match is used as is. Otherwise the menu picker
        infers which candidate the user meant from what it knows about the chain, and may
        suggest the chain's own wording for one more search. Without a picker, or when it
        picks nothing, the closest word match above the floor is used, as before.
        """
        asked = match.asked_text(identity)
        # No amount said: the serving that is the named thing itself ("personal pizza" is a pizza,
        # not the provider's first listed slice).
        named = re.findall(r"[a-z]+", food_words.normalize(identity.name).casefold())
        prefer = portion.text.strip() or (named[-1] if named else "")
        pool: dict[str, tuple[float, int, Mapping[str, Any]]] = {}

        async def search(text: str) -> None:
            hits = await scope.call(("hits", text.casefold()), lambda: self.sources.hits(text), default=[])
            for key in _candidates(identity, asked, hits):
                food_id = str(key[2]["food_id"])
                if food_id not in pool or key[0] > pool[food_id][0]:
                    pool[food_id] = (key[0], len(pool), key[2])

        async def fetch(hit: Mapping[str, Any]) -> Found | None:
            food_id = str(hit["food_id"])
            found = await scope.call(("food", food_id, prefer),
                                     lambda: self.sources.food(food_id, query, hit, prefer=prefer))
            food = with_serving_unit(to_resolved_food(found) if isinstance(found, Mapping) else None, query, portion)
            return _branded(food, identity, "verified") if food is not None else None

        for text in search_queries(identity, query):
            await search(text)
            if any(score >= CLEAR_MATCH for score, _o, _h in pool.values()):
                break

        clear = [hit for score, _o, hit in _best_first(pool) if score >= CLEAR_MATCH]
        for hit in clear[:_MAX_FETCHES]:
            found = await fetch(hit)
            if found is not None:
                return found

        if self.picker is not None and menu_pick.enabled():
            said = {"brand": identity.brand, "item": identity.name, "variant": identity.variant,
                    "preparation": identity.preparation, "amount_words": portion.text}
            for _round in range(2):
                offered = [hit for _s, _o, hit in _best_first(pool)][:menu_pick.MAX_CANDIDATES]
                pick = await self._pick(said, offered) if offered or _round == 0 else menu_pick.NO_PICK
                chosen = next((hit for hit in offered if str(hit["food_id"]) == pick.food_id), None)
                if chosen is not None:  # only a record code offered, whatever the model says
                    found = await fetch(chosen)
                    if found is not None:
                        return found
                    break
                if pick.food_id is not None:
                    break
                if not pick.search:
                    break
                for text in pick.search:
                    await search(text)

        for hit in [hit for score, _o, hit in _best_first(pool) if score >= match.MIN_CLOSENESS][:_MAX_FETCHES]:
            found = await fetch(hit)
            if found is not None:
                return found
        return None

    async def _pick(self, said: Mapping[str, Any], offered: list[Mapping[str, Any]]) -> menu_pick.Pick:
        candidates = [{"id": str(hit["food_id"]), "name": str(hit.get("food_name") or ""),
                       "brand": str(hit.get("brand_name") or "")} for hit in offered]
        try:
            return await self.picker(said, candidates)
        except Exception as exc:  # noqa: BLE001 - the picker is an aid; the code ranking still answers
            logger.info("food retrieval: menu pick failed: %s", type(exc).__name__)
            return menu_pick.NO_PICK

    # ------------------------------------------------------------ restoring a draft

    async def refresh(self, item: DraftItemState, scope: Scope) -> ResolvedFood | None:
        """Restore a provider panel that may not be stored in a draft. Same food, same source."""
        food = item.resolved
        if food is None:
            return None
        if food.provider == "FatSecret" and food.external_id:
            query = identity_query(item.identity)
            hit = {"food_id": food.external_id, "food_name": item.identity.name, "brand_name": item.identity.brand or ""}
            shown = food.serving_basis.text  # the same serving as was shown, so the multiplier keeps its meaning
            found = await scope.call(("food", food.external_id, shown),
                                     lambda: self.sources.food(food.external_id, query, hit, prefer=shown))
            fresh = to_resolved_food(found) if isinstance(found, Mapping) else None
        else:
            fresh = (await self.find_on_web(item.identity, item.portion, scope)).food
        # The same serving unit as when it was shown, so the stored multiplier still means the same amount.
        fresh = with_serving_unit(fresh, identity_query(item.identity), item.portion)
        if fresh is None:
            return None
        return food.model_copy(update={"panel": fresh.panel, "serving_basis": fresh.serving_basis})


_PLAIN_DEFAULTS = frozenset({"cheese", "plain", "original", "classic", "regular"})
_SIZE_OR_INCH = re.compile(r"\b\d+(?:\.\d+)? inch\b|\b(?:small|medium|large|xl)\b", re.IGNORECASE)


def search_queries(identity: FoodIdentity, query: str) -> list[str]:
    """The provider searches for one named product, most specific first: as said, then without
    sizes and diameters, then the brand and the food's head word."""
    out = [query]
    simpler = " ".join(_SIZE_OR_INCH.sub(" ", food_words.normalize(query)).split())
    out.append(simpler)
    words = re.findall(r"[A-Za-z]+", food_words.normalize(identity.name))
    if identity.brand and words:
        out.append(f"{identity.brand} {words[-1]}")
    return list({text.casefold(): text for text in out if text.strip()}.values())


def _candidates(identity: FoodIdentity, asked: str, hits: Any) -> list[tuple[float, int, Mapping[str, Any]]]:
    """Hits under the same brand that are not a different food, with their word score. A plain
    version (cheese, original) gets a hair more when the user named no topping or flavor."""
    out = []
    asked_words = match.tokens(asked)
    for order, hit in enumerate(hits or []):
        if not isinstance(hit, Mapping) or not hit.get("food_id"):
            continue
        name, brand = str(hit.get("food_name") or ""), str(hit.get("brand_name") or "")
        if identity.brand and not _same_brand(identity.brand, brand, name):
            continue
        if match.conflict(asked, name):
            continue
        plain = bool((match.tokens(name) - asked_words) & _PLAIN_DEFAULTS)
        out.append((round(match.closeness(asked, name, identity.brand or brand), 4) + (0.001 if plain else 0.0),
                    order, hit))
    return out


def _best_first(pool: Mapping[str, tuple[float, int, Mapping[str, Any]]]) -> list[tuple[float, int, Mapping[str, Any]]]:
    return sorted(pool.values(), key=lambda entry: (-entry[0], entry[1]))


def found_text(found: Mapping[str, Any]) -> str:
    return " ".join(str(found.get(key) or "") for key in ("name", "basis"))


def _same_brand(asked: str, brand: str, name: str) -> bool:
    wanted = "".join(sorted(match.tokens(asked)))
    if not wanted:
        return True
    return wanted in "".join(sorted(match.tokens(brand))) or match.tokens(asked) <= match.tokens(f"{brand} {name}")


__all__ = ["Found", "Scope", "Retrieval", "lane_of", "user_words", "web_query", "MAX_CALLS_PER_TURN"]
