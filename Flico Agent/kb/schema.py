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
    parking: Optional[int] = None
    deposit_months: Optional[int] = None
    advance_months: Optional[int] = None
    min_lease_months: Optional[int] = None
    key_features: List[str] = Field(default_factory=list)
    description: str = ""


class QueryFilters(BaseModel):
    transaction: Optional[str] = None
    property_type: Optional[str] = None
    zone: Optional[int] = None
    min_bedrooms: Optional[int] = None
    min_rent: Optional[float] = None
    max_rent: Optional[float] = None
