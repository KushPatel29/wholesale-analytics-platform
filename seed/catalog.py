"""
Reference data for the synthetic wholesale distributor.

Everything the generator needs to invent a believable perishable-goods
business lives here as plain data, so the shape of the demo company can be
inspected and changed without reading generator code.

Money is in CAD. Weights are in pounds, because that is what the trade uses
even in metric countries.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Billing basis, mirroring the source ERP's UnitOfBillingId.
# 3 means "billed on the weight actually shipped" (catch weight); anything
# else means "billed per unit/case at a fixed price".
BILL_BY_WEIGHT = 3
BILL_BY_UNIT = 1


@dataclass(frozen=True)
class ProteinGroup:
    """A merchandising category and how it behaves commercially."""

    name: str
    # Typical landed cost per lb, and the spread we sell it at.
    cost_per_lb: float
    markup: float
    # Catch-weight share: primals and whole fish are weighed, portions are not.
    catch_weight_share: float
    # Processing yield: what is left after trimming and portioning.
    yield_pct: float
    # Multiplicative demand factor by calendar month (Jan..Dec).
    seasonality: tuple[float, ...]
    # Share of total order lines this group should attract.
    weight: float


# Seasonality curves are deliberately opinionated: poultry peaks into the
# winter holidays, seafood peaks over summer, lamb peaks at Easter, and
# barbecue cuts follow the patio season.
PROTEIN_GROUPS: tuple[ProteinGroup, ...] = (
    ProteinGroup(
        name="Beef",
        cost_per_lb=7.40,
        markup=1.32,
        catch_weight_share=0.72,
        yield_pct=0.68,
        seasonality=(0.88, 0.86, 0.94, 1.00, 1.12, 1.20, 1.24, 1.18, 1.05, 0.98, 0.94, 1.02),
        weight=0.31,
    ),
    ProteinGroup(
        name="Pork",
        cost_per_lb=3.85,
        markup=1.29,
        catch_weight_share=0.45,
        yield_pct=0.74,
        seasonality=(0.92, 0.90, 0.96, 1.02, 1.10, 1.16, 1.18, 1.12, 1.04, 0.98, 0.96, 1.06),
        weight=0.19,
    ),
    ProteinGroup(
        name="Poultry",
        cost_per_lb=3.10,
        markup=1.21,
        catch_weight_share=0.30,
        yield_pct=0.71,
        seasonality=(0.90, 0.88, 0.92, 0.96, 0.98, 1.00, 1.00, 1.00, 1.06, 1.22, 1.34, 1.28),
        weight=0.17,
    ),
    ProteinGroup(
        name="Lamb",
        cost_per_lb=9.60,
        markup=1.31,
        catch_weight_share=0.66,
        yield_pct=0.64,
        seasonality=(0.86, 0.92, 1.18, 1.30, 1.06, 0.98, 0.94, 0.92, 0.96, 1.00, 1.02, 1.14),
        weight=0.07,
    ),
    ProteinGroup(
        name="Seafood",
        cost_per_lb=11.20,
        markup=1.27,
        catch_weight_share=0.80,
        yield_pct=0.58,
        seasonality=(0.84, 0.88, 0.94, 1.02, 1.14, 1.26, 1.32, 1.28, 1.10, 0.96, 0.90, 0.94),
        weight=0.13,
    ),
    ProteinGroup(
        name="Charcuterie",
        cost_per_lb=13.50,
        markup=1.42,
        catch_weight_share=0.18,
        yield_pct=0.92,
        seasonality=(0.94, 0.96, 0.98, 1.00, 1.02, 1.04, 1.02, 1.00, 1.04, 1.10, 1.24, 1.38),
        weight=0.08,
    ),
    ProteinGroup(
        name="Game",
        cost_per_lb=12.80,
        markup=1.34,
        catch_weight_share=0.62,
        yield_pct=0.61,
        seasonality=(0.92, 0.90, 0.90, 0.92, 0.94, 0.94, 0.92, 0.96, 1.14, 1.28, 1.22, 1.06),
        weight=0.05,
    ),
)


# Cut names per protein. Paired with a grade/preparation to build SKU names.
CUTS: dict[str, tuple[str, ...]] = {
    "Beef": (
        "Ribeye", "Striploin", "Tenderloin", "Top Sirloin", "Flat Iron", "Brisket",
        "Short Rib", "Chuck Roll", "Flank", "Skirt", "Ground Chuck", "Osso Buco",
    ),
    "Pork": (
        "Belly", "Loin", "Tenderloin", "Shoulder Butt", "Back Rib", "Side Rib",
        "Ground Pork", "Hock", "Jowl", "Collar",
    ),
    "Poultry": (
        "Breast", "Thigh", "Drumstick", "Wing", "Whole Bird", "Ground Chicken",
        "Boneless Thigh", "Supreme", "Leg Quarter",
    ),
    "Lamb": (
        "Rack", "Leg", "Shoulder", "Shank", "Loin Chop", "Ground Lamb", "Neck",
    ),
    "Seafood": (
        "Salmon Fillet", "Halibut Fillet", "Sablefish", "Spot Prawn", "Sea Scallop",
        "Albacore Loin", "Ling Cod", "Dungeness Crab", "Side Stripe Shrimp",
    ),
    "Charcuterie": (
        "Prosciutto", "Coppa", "Bresaola", "Duck Rillette", "Pork Terrine",
        "Saucisson", "Pancetta", "Guanciale", "Lardo",
    ),
    "Game": (
        "Venison Loin", "Bison Ribeye", "Duck Breast", "Elk Striploin",
        "Rabbit Saddle", "Wild Boar Shoulder", "Quail",
    ),
}

GRADES: tuple[str, ...] = ("AAA", "AA", "Prime", "Choice", "Select", "Ungraded")
PREPARATIONS: tuple[str, ...] = (
    "Fresh", "Frozen", "Vac Pack", "Portioned", "Whole", "Trimmed", "Bone-In", "Boneless",
)


@dataclass(frozen=True)
class Region:
    name: str
    # Share of customers based here.
    weight: float
    # Typical transit days from the plant; drives the delivery-performance story.
    transit_days: float
    # Cities used for customer addresses.
    cities: tuple[str, ...]


REGIONS: tuple[Region, ...] = (
    Region("Lower Mainland", 0.34, 1.0, ("Vancouver", "Burnaby", "Richmond", "Surrey", "Coquitlam")),
    Region("Vancouver Island", 0.16, 2.0, ("Victoria", "Nanaimo", "Courtenay", "Duncan")),
    Region("Interior", 0.14, 2.5, ("Kelowna", "Kamloops", "Vernon", "Penticton", "Revelstoke")),
    Region("Northern BC", 0.06, 3.5, ("Prince George", "Terrace", "Smithers")),
    Region("Alberta", 0.17, 3.0, ("Calgary", "Edmonton", "Banff", "Canmore")),
    Region("Prairies", 0.08, 4.0, ("Saskatoon", "Regina", "Winnipeg")),
    Region("Pacific Northwest", 0.05, 3.0, ("Seattle", "Bellingham", "Portland")),
)

PROVINCE_BY_REGION: dict[str, str] = {
    "Lower Mainland": "BC",
    "Vancouver Island": "BC",
    "Interior": "BC",
    "Northern BC": "BC",
    "Alberta": "AB",
    "Prairies": "MB",
    "Pacific Northwest": "WA",
}


@dataclass(frozen=True)
class CustomerSegment:
    """How a class of buyer behaves: order size, cadence and price sensitivity."""

    name: str
    is_retail: bool
    weight: float
    # Orders per month, and lines per order.
    orders_per_month: float
    lines_per_order: float
    # Multiplier on list price. Distributors buy the cheapest.
    price_index: float
    # Multiplier on order quantity.
    size_index: float


CUSTOMER_SEGMENTS: tuple[CustomerSegment, ...] = (
    CustomerSegment("Independent Restaurant", False, 0.40, 3.4, 4.2, 1.00, 1.00),
    CustomerSegment("Restaurant Group", False, 0.12, 6.2, 7.5, 0.94, 2.60),
    CustomerSegment("Hotel & Banquet", False, 0.09, 2.6, 9.0, 0.96, 3.10),
    CustomerSegment("Butcher Shop", True, 0.14, 4.1, 5.0, 0.97, 1.70),
    CustomerSegment("Grocery Retail", True, 0.13, 5.0, 8.5, 0.90, 4.20),
    CustomerSegment("Distributor", False, 0.05, 3.0, 12.0, 0.82, 9.50),
    CustomerSegment("Institutional", False, 0.07, 1.8, 6.0, 0.92, 3.80),
)


@dataclass(frozen=True)
class ShippingMethod:
    name: str
    carrier: str
    weight: float
    # Added days on top of the region's transit time.
    extra_days: float
    # Probability the delivery misses its expected date.
    late_rate: float


# "Third Party LTL" is deliberately the worst performer: it is the only method
# available to the far regions, which is what makes the late-delivery story a
# routing problem rather than a carrier problem.
SHIPPING_METHODS: tuple[ShippingMethod, ...] = (
    ShippingMethod("Own Fleet AM", "In-House", 0.34, 0.0, 0.03),
    ShippingMethod("Own Fleet PM", "In-House", 0.22, 0.0, 0.05),
    ShippingMethod("Refrigerated Courier", "ColdLink", 0.18, 0.5, 0.07),
    ShippingMethod("Third Party LTL", "Northbound Freight", 0.14, 1.5, 0.24),
    ShippingMethod("Customer Pickup", "Pickup", 0.08, 0.0, 0.01),
    ShippingMethod("Air Freight", "AirCargo West", 0.04, 0.0, 0.09),
)


SUPPLIER_PREFIXES: tuple[str, ...] = (
    "Cascade", "Fraser", "Harbour", "Ridgeline", "Silverbrook", "Kootenay",
    "Highfield", "Northwind", "Copper Creek", "Stonebridge", "Alderwood",
    "Blackpine", "Marbleworks", "Sandhill", "Grayrock", "Elkhorn", "Bayfield",
    "Windrow", "Thornbury", "Larkspur",
)

SUPPLIER_SUFFIXES: tuple[str, ...] = (
    "Meats", "Provisions", "Packing Co", "Ranch", "Farms", "Seafoods",
    "Abattoir", "Livestock", "Fisheries", "Curing House",
)


@dataclass
class SalesRep:
    name: str
    # Share of the customer book. Deliberately uneven.
    book_share: float
    # Regions this rep covers.
    regions: tuple[str, ...] = field(default_factory=tuple)


# Rep 1 carries a disproportionate book, which is the concentration risk the
# sales-rep dashboard is supposed to surface.
SALES_REPS: tuple[SalesRep, ...] = (
    SalesRep("Dana Whitfield", 0.26, ("Lower Mainland", "Vancouver Island")),
    SalesRep("Marcus Oyelaran", 0.16, ("Lower Mainland",)),
    SalesRep("Priya Raghunathan", 0.14, ("Interior", "Northern BC")),
    SalesRep("Tomasz Bielski", 0.12, ("Alberta",)),
    SalesRep("Renee Chartrand", 0.11, ("Vancouver Island", "Lower Mainland")),
    SalesRep("Sam Okonkwo", 0.09, ("Prairies", "Alberta")),
    SalesRep("Hana Sasaki", 0.07, ("Pacific Northwest", "Lower Mainland")),
    SalesRep("Elliot Vance", 0.05, ("Interior",)),
)


ORDER_STATUSES: tuple[str, ...] = ("packed",)

# Business-name vocabulary for customers, kept separate from suppliers so the
# two never collide.
CUSTOMER_FIRST: tuple[str, ...] = (
    "Copper", "Lantern", "Alder", "Marrow", "Quarry", "Salt", "Ember", "Tide",
    "Fennel", "Birch", "Cedar", "Harvest", "Anchor", "Foundry", "Meridian",
    "Juniper", "Slate", "Thistle", "Vintage", "Willow", "Granite", "Nine Mile",
    "Union", "Basalt", "Clover", "Mariner", "Orchard", "Pike", "Rook", "Sable",
)

CUSTOMER_SECOND: tuple[str, ...] = (
    "Kitchen", "Table", "Grill", "Butcher", "Market", "Bistro", "Tavern",
    "Larder", "Provisions", "Smokehouse", "Chophouse", "Public House",
    "Fine Foods", "Deli", "Brasserie", "Supper Club", "Canteen", "Eatery",
)
