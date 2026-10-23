"""Field-level observation provenance and deterministic source conflict resolution."""
from __future__ import annotations

from src.ingest.normalize import SOURCE_FIELDS, clean_text, official_url, parse_deadline

SECTION_FIELDS = {"support_target", "selection_criteria", "support_content", "how_to_apply"}


def _observation(source: str, row: dict | None, key: str, *, fresh: bool) -> dict:
    if row is None or key not in row:
        state = "unavailable" if row is None else ("fresh_missing" if fresh else "retained_missing")
        return {"source": source, "observation": state, "value": None, "normalized_value": ""}
    value = clean_text(row[key])
    if key == SOURCE_FIELDS["detail_url"]:
        value = official_url(value)
    state = "value" if value != "" else ("empty" if row[key] is None or clean_text(row[key]) == "" else "invalid")
    return {"source": source, "observation": ("fresh_" if fresh else "retained_") + state,
            "value": row[key], "normalized_value": value}


def merge_fields(listing: dict, detail: dict | None, previous: dict, *,
                 previous_provenance: dict | None = None) -> tuple[dict, dict, list[dict]]:
    """Fresh meaningful common fields win over historical fallback; explicit empties clear.

    No source-update-time ordering is claimed. If both current sources disagree,
    listing wins identity/provenance fields. Deadline conflicts prefer the earlier
    unambiguous date; ambiguous current periods prevent an invented exact deadline.
    Successful detail section omissions continue to mean source-level removal.
    """
    merged = {"서비스ID": listing["서비스ID"]}
    provenance, conflicts = {}, []
    for field, key in SOURCE_FIELDS.items():
        if field == "service_id":
            continue
        current_list = _observation("serviceList", listing, key, fresh=True)
        current_detail = _observation("serviceDetail", detail, key, fresh=True)
        historic_detail = _observation("serviceDetail", previous.get("serviceDetail"), key, fresh=False)
        historic_list = _observation("serviceList", previous.get("serviceList"), key, fresh=False)
        candidates = [current_list, current_detail, historic_detail, historic_list]
        prior = (previous_provenance or {}).get(field, {})
        last_resolved = None
        if "raw_value" in prior:
            last_resolved = _observation(prior["source"], {key: prior["raw_value"]}, key, fresh=False)
            candidates.append(last_resolved)
        if field in SECTION_FIELDS and detail is not None:
            chosen = current_detail
            if key not in detail:
                chosen = {**current_detail, "observation": "fresh_missing", "value": None}
                candidates[1] = chosen
            reason = "current_detail_section_observation"
        else:
            meaningful = [c for c in candidates[:2] if c["observation"] == "fresh_value"]
            if meaningful:
                chosen = meaningful[0]  # explicit listing preference for common fields
                reason = "current_value_over_historical_fallback"
                if len(meaningful) == 2 and meaningful[0]["normalized_value"] != meaningful[1]["normalized_value"]:
                    reason = "current_listing_precedence_source_order_unverified"
                    if field == "application_period":
                        deadlines = [parse_deadline(c["normalized_value"]) for c in meaningful]
                        if all(d is not None for d in deadlines):
                            chosen = meaningful[0] if deadlines[0] <= deadlines[1] else meaningful[1]
                            reason = "current_deadline_conflict_earlier_end_requires_review"
                        else:
                            chosen = next(c for c, d in zip(meaningful, deadlines) if d is None)
                            reason = "current_period_conflict_ambiguous_requires_review"
            else:
                observed = [c for c in candidates[:2] if c["observation"] in ("fresh_empty", "fresh_invalid")]
                if observed:
                    chosen, reason = observed[0], "current_empty_or_invalid_clears_historical"
                else:
                    if last_resolved is not None:
                        # Persist the previous field decision, including explicit
                        # removal, even after later source rows omit that key.
                        chosen, reason = last_resolved, "last_resolved_field_no_current_observation"
                    else:
                        retained = [c for c in candidates[2:] if c["observation"] == "retained_value"]
                        chosen = retained[0] if retained else next(
                            (c for c in candidates[2:] if c["observation"] in ("retained_empty", "retained_invalid")), historic_detail)
                        reason = "historical_fallback_no_current_field" if retained else "no_current_or_valid_historical_value"
        # clean_service owns normalization. Passing normalized entity text through
        # HTMLParser twice could reinterpret escaped literal tags as real markup.
        merged[key] = chosen["value"]
        decision = {"source": chosen["source"], "observation": chosen["observation"],
                    "chosen_value": chosen["normalized_value"], "reason": reason,
                    "raw_value": chosen["value"],
                    "inputs": [{"source": c["source"], "observation": c["observation"]} for c in candidates]}
        if (chosen["observation"].startswith("retained_") and chosen["source"] == prior.get("source")
                and chosen["value"] == prior.get("raw_value") and prior.get("source_acquisition")):
            decision["source_acquisition"] = prior["source_acquisition"]
        provenance[field] = decision
        available = [c for c in candidates if c["observation"] in
                     ("fresh_value", "fresh_empty", "fresh_invalid", "retained_value", "retained_empty", "retained_invalid")
                     or (c is chosen and c["observation"] == "fresh_missing" and field in SECTION_FIELDS)]
        if len({c["normalized_value"] for c in available}) > 1:
            conflicts.append({"field": field, "source_field": key, "candidates": available,
                              **decision, "requires_review": len([c for c in available
                                  if c["observation"].startswith("fresh_")]) > 1})
    return merged, provenance, conflicts
