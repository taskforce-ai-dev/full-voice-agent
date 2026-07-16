from typing import List

from kb.schema import Property


class ContextFormatter:
    @staticmethod
    def _rent(p: Property) -> str:
        if p.rent_on_request or p.rent_amount is None:
            return "rent on request — a consultant will confirm on follow-up"
        period = p.rent_period or "month"
        return f"Rs {int(p.rent_amount):,} per {period}"

    @staticmethod
    def _line(p: Property) -> str:
        # The stored prose is the fullest rendering of a listing: it carries the
        # street, amenities, lease terms, bathrooms and availability that the
        # normalized columns drop. Prefer it, and synthesize a line only for rows
        # that have no prose.
        if p.description:
            return f"[{p.id}] {p.description}"
        loc = f"Colombo {p.zone}" if p.zone else (p.area or "")
        where = f" at {p.building}" if p.building else ""
        beds = f"{p.bedrooms}-bedroom " if p.bedrooms else ""
        furnish = f"{p.furnishing} " if p.furnishing else ""
        area = f", {p.floor_area_sqft} sq ft" if p.floor_area_sqft else ""
        feats = f" Features: {', '.join(p.key_features)}." if p.key_features else ""
        return (
            f"[{p.id}] Rodrigo Realtors has a {beds}{furnish}{p.property_type} "
            f"for {p.transaction}{where} in {loc}{area}. {ContextFormatter._rent(p)}.{feats}"
        )

    @staticmethod
    def format(props: List[Property]) -> str:
        if not props:
            return ""
        return "\n\n".join(ContextFormatter._line(p) for p in props)
