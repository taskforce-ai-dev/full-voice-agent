from typing import List, Optional
from pydantic import BaseModel, Field


class Property(BaseModel):
    id: str
    transaction: str = Field(description="'rent' or 'sale'")
    property_type: str = Field(description="apartment | house | commercial | land")
    zone: Optional[int] = Field(default=None, description="Colombo postal zone 1-10")
    area: str = ""
    building: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    rent_amount: Optional[float] = None
    rent_period: Optional[str] = Field(default=None, description="'month' or 'day'")
    rent_on_request: bool = False
    sale_price: Optional[float] = None
    furnishing: Optional[str] = Field(default=None, description="furnished | semi | unfurnished")
    floor_area_sqft: Optional[int] = None
    land_perches: Optional[int] = None
    parking: Optional[int] = None
    deposit_months: Optional[int] = None
    advance_months: Optional[int] = None
    min_lease_months: Optional[int] = None
    street: Optional[str] = Field(
        default=None, description="street or road, e.g. 'Park Street'")
    available: str = Field(default="now", description="'now' | 'soon' | free text")
    key_features: List[str] = Field(
        default_factory=list,
        description="amenities as DATA. These used to live only inside the prose, "
                    "so they were invisible to any filter -- 'something with a "
                    "pool' could never be answered structurally.")
    commentary: str = Field(
        default="",
        description="human-authored colour for this listing, rendered verbatim "
                    "into the prose. This is where personality lives -- never an "
                    "LLM paraphrase, which would make the data probabilistic.")
    description: str = Field(
        default="",
        description="the prose the LLM reads. GENERATED from the fields above by "
                    "kb/prose.py -- a derived artifact, never the source of truth.")


class QueryFilters(BaseModel):
    transaction: Optional[str] = None
    property_type: Optional[str] = None
    zone: Optional[int] = None
    bedrooms: Optional[int] = Field(
        default=None, description="exact bedroom count: 'a two bedroom apartment'")
    min_bedrooms: Optional[int] = Field(
        default=None, description="bedroom floor: 'at least two bedrooms', '2+'")
    min_rent: Optional[float] = None
    max_rent: Optional[float] = None
    max_rent_exclusive: bool = Field(
        default=False,
        description="'under 300k' excludes a 300k listing; 'up to 300k' includes it")
