"""
Reference data for the synthetic retail chain.

Everything the generator needs to invent a believable mass-retail business
lives here as plain data, so the shape of the demo company can be inspected and
changed without reading generator code.

The demo company is **Northgate Retail Group** - a supercenter chain running
merchandising and replenishment analytics over its own store network. Stores
are the demand points, distribution centres and vendors are the supply, and
market managers own the store book.

Money is in USD.

A note on units, because it explains the whole schema
----------------------------------------------------
A supercenter sells two ways at once, and the source system has always modelled
that with `UnitOfBillingId`:

* **Weighed** items - produce, meat, deli, bakery - are priced per pound and
  ring up whatever the scale says. `UnitOfBillingId == 3`.
* **Each** items - electronics, apparel, packaged grocery - are priced per unit
  at a fixed price.

The warehouse derives revenue from that distinction rather than storing it, so
the generator must never precompute it. That is why the fields below are
expressed as a cost per *selling unit*: a pound for weighed departments, an each
for the rest.

Naming
------
Warehouse column names still come from the source system, which predates the
current merchandising vocabulary - `ProteinType` carries the department,
`YieldPct` carries sell-through, `CostPerLb` carries cost per selling unit.
Renaming a source system's columns to match a re-org is how you break every
downstream report, so the columns stay and the mapping is documented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Billing basis, mirroring the source system's UnitOfBillingId.
# 3 means "billed on the weight actually rung up" (scale items); anything else
# means "billed per each at a fixed price".
BILL_BY_WEIGHT = 3
BILL_BY_UNIT = 1


@dataclass(frozen=True)
class Department:
    """A merchandising department and how it behaves commercially."""

    name: str
    # Typical landed cost per selling unit (a pound for weighed departments,
    # an each for the rest), and the spread it is marked up at.
    unit_cost: float
    markup: float
    # Share of items in this department that are sold by weight rather than
    # by each. Fresh is nearly all scale; electronics is none of it.
    weighed_share: float
    # Sell-through: the share of units bought that are sold at a price that
    # counts. Fresh loses it to shrink, apparel and seasonal lose it to
    # markdown, and packaged goods keep nearly all of it.
    sell_through: float
    # Multiplicative demand factor by calendar month (Jan..Dec).
    seasonality: tuple[float, ...]
    # Share of total order lines this department should attract.
    weight: float
    # Probability a replenishment line cannot be filled complete. Fresh runs
    # short because it is grown rather than manufactured; seasonal runs short
    # because the buy is committed months before the demand is known; packaged
    # grocery almost never does.
    short_ship_rate: float = 0.04
    # When a line does go short, the share of the order that typically arrives.
    # A produce shortfall is usually partial; an electronics allocation is
    # frequently most of the order or none of it.
    typical_fill_when_short: float = 0.72


# Department mix is roughly a supercenter's: grocery and fresh carry the volume
# at thin margins, general merchandise carries the margin at lower volume.
#
# Two stories are planted deliberately, because a dashboard that finds nothing
# is not worth looking at:
#
#   Electronics is a large share of revenue at the thinnest margin in the
#   chain, so it drags blended margin down while looking like a growth engine
#   on a revenue chart.
#
#   Apparel and Toys & Seasonal have the worst sell-through, which is a
#   markdown problem rather than a demand problem - it shows up in margin and
#   in the SKU watchlist, not in the sales line.
DEPARTMENTS: tuple[Department, ...] = (
    Department(
        name="Grocery",
        unit_cost=3.20,
        markup=1.26,
        weighed_share=0.02,
        sell_through=0.985,
        seasonality=(0.97, 0.94, 0.99, 1.00, 1.02, 1.03, 1.04, 1.03, 1.00, 1.01, 1.08, 1.14),
        weight=0.3,
        short_ship_rate=0.018,
        typical_fill_when_short=0.8,
    ),
    Department(
        name="Fresh & Produce",
        unit_cost=2.10,
        markup=1.38,
        weighed_share=0.78,
        sell_through=0.88,
        seasonality=(0.90, 0.89, 0.95, 1.01, 1.10, 1.18, 1.22, 1.19, 1.06, 0.98, 0.96, 1.02),
        weight=0.16,
        short_ship_rate=0.115,
        typical_fill_when_short=0.62,
    ),
    Department(
        name="Dairy & Frozen",
        unit_cost=3.05,
        markup=1.27,
        weighed_share=0.06,
        sell_through=0.960,
        seasonality=(0.95, 0.93, 0.97, 1.00, 1.05, 1.12, 1.16, 1.12, 1.01, 0.97, 0.98, 1.06),
        weight=0.13,
        short_ship_rate=0.035,
        typical_fill_when_short=0.75,
    ),
    Department(
        name="Meat & Seafood",
        unit_cost=6.40,
        markup=1.30,
        weighed_share=0.85,
        sell_through=0.910,
        seasonality=(0.88, 0.87, 0.94, 1.02, 1.14, 1.22, 1.26, 1.18, 1.02, 0.96, 0.96, 1.08),
        weight=0.1,
        short_ship_rate=0.072,
        typical_fill_when_short=0.68,
    ),
    Department(
        name="Health & Wellness",
        unit_cost=7.80,
        markup=1.42,
        weighed_share=0.00,
        sell_through=0.990,
        seasonality=(1.12, 1.06, 1.00, 0.97, 0.95, 0.94, 0.94, 0.98, 1.02, 1.04, 1.00, 0.98),
        weight=0.09,
        short_ship_rate=0.026,
        typical_fill_when_short=0.78,
    ),
    Department(
        name="Household Essentials",
        unit_cost=5.10,
        markup=1.31,
        weighed_share=0.00,
        sell_through=0.990,
        seasonality=(1.02, 0.98, 1.02, 1.03, 1.02, 1.00, 0.99, 1.00, 1.01, 1.00, 0.98, 0.95),
        weight=0.09,
        short_ship_rate=0.022,
        typical_fill_when_short=0.82,
    ),
    Department(
        name="Apparel",
        unit_cost=9.50,
        markup=1.72,
        weighed_share=0.00,
        sell_through=0.820,
        seasonality=(0.82, 0.86, 1.06, 1.14, 1.06, 0.96, 0.92, 1.16, 1.12, 0.94, 0.98, 1.10),
        weight=0.05,
        short_ship_rate=0.058,
        typical_fill_when_short=0.7,
    ),
    Department(
        name="Electronics",
        unit_cost=84.00,
        markup=1.14,
        weighed_share=0.00,
        sell_through=0.940,
        seasonality=(0.86, 0.82, 0.88, 0.90, 0.92, 0.94, 0.96, 1.02, 1.00, 1.02, 1.46, 1.42),
        weight=0.025,
        short_ship_rate=0.094,
        typical_fill_when_short=0.55,
    ),
    Department(
        name="Home & Kitchen",
        unit_cost=14.50,
        markup=1.55,
        weighed_share=0.00,
        sell_through=0.930,
        seasonality=(0.94, 0.90, 0.98, 1.04, 1.10, 1.06, 1.00, 0.98, 1.02, 1.02, 1.06, 1.14),
        weight=0.035,
        short_ship_rate=0.04,
        typical_fill_when_short=0.74,
    ),
    Department(
        name="Toys & Seasonal",
        unit_cost=11.00,
        markup=1.48,
        weighed_share=0.00,
        sell_through=0.790,
        seasonality=(0.62, 0.60, 0.72, 0.86, 0.92, 1.06, 1.02, 0.94, 0.90, 1.06, 1.72, 2.10),
        weight=0.02,
        short_ship_rate=0.132,
        typical_fill_when_short=0.58,
    ),
)

# The generator was written against the previous merchandising vocabulary and
# several tests import this name. Same objects, older label.
ProteinGroup = Department
PROTEIN_GROUPS = DEPARTMENTS


# Item types per department. Paired with a brand tier and a pack size to build
# SKU names.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Grocery": (
        "Breakfast Cereal", "Pasta", "Cooking Oil", "Canned Soup", "Snack Crackers",
        "Coffee", "Bottled Water", "Soda 12pk", "Rice", "Baking Mix",
        "Condiments", "Baby Formula",
    ),
    "Fresh & Produce": (
        "Bananas", "Apples", "Salad Greens", "Tomatoes", "Berries", "Potatoes",
        "Citrus", "Avocados", "Onions", "Fresh Herbs",
    ),
    "Dairy & Frozen": (
        "Whole Milk", "Shredded Cheese", "Greek Yogurt", "Butter", "Ice Cream",
        "Frozen Pizza", "Frozen Vegetables", "Eggs", "Frozen Entree",
    ),
    "Meat & Seafood": (
        "Ground Beef", "Chicken Breast", "Pork Chops", "Bacon", "Salmon Fillet",
        "Shrimp", "Deli Turkey", "Ribeye Steak", "Rotisserie Chicken",
    ),
    "Health & Wellness": (
        "Pain Relief", "Vitamins", "Shampoo", "Toothpaste", "Cold & Flu",
        "Skin Care", "Allergy Relief", "First Aid", "Razors",
    ),
    "Household Essentials": (
        "Laundry Detergent", "Paper Towels", "Bath Tissue", "Dish Soap",
        "Trash Bags", "Surface Cleaner", "Air Freshener", "Food Storage",
    ),
    "Apparel": (
        "Mens Tee", "Womens Denim", "Kids Hoodie", "Socks 6pk", "Activewear Legging",
        "Sleepwear Set", "Work Boot", "Baby Onesie", "Winter Jacket",
    ),
    "Electronics": (
        "4K Smart TV", "Bluetooth Headphones", "Tablet", "Streaming Stick",
        "Wireless Router", "Game Console", "Smart Watch", "Portable Speaker",
    ),
    "Home & Kitchen": (
        "Bath Towel Set", "Bed Sheet Set", "Cookware Set", "Air Fryer",
        "Storage Bin", "Table Lamp", "Area Rug", "Coffee Maker",
    ),
    "Toys & Seasonal": (
        "Building Blocks", "Action Figure", "Board Game", "Ride-On Toy",
        "Patio Chair", "String Lights", "Cooler", "Artificial Tree", "Beach Set",
    ),
}
# Older name, same data.
CUTS = CATEGORIES

# Brand tier. "Northgate Value" is the chain's opening-price private label and
# "Northgate Select" its premium one - a private-label ladder is most of how a
# mass retailer defends margin, so the demo has one.
BRAND_TIERS: tuple[str, ...] = (
    "Northgate Value", "Northgate Select", "National Brand",
    "Premium Label", "Everyday Basics", "Exclusive Brand",
)
GRADES = BRAND_TIERS

PACK_SIZES: tuple[str, ...] = (
    "Single", "2-Pack", "Family Size", "Club Pack", "Travel Size",
    "Value Bundle", "Multipack", "Bulk",
)
PREPARATIONS = PACK_SIZES


@dataclass(frozen=True)
class Region:
    name: str
    # Share of stores in this region.
    weight: float
    # Typical transit days from the serving distribution centre; drives the
    # on-time replenishment story.
    transit_days: float
    # Cities used for store addresses.
    cities: tuple[str, ...]


REGIONS: tuple[Region, ...] = (
    Region("Texas & Gulf", 0.22, 1.0, ("Dallas", "Houston", "San Antonio", "Austin", "Baton Rouge")),
    Region("Southeast", 0.19, 1.5, ("Atlanta", "Charlotte", "Nashville", "Orlando", "Birmingham")),
    Region("Midwest", 0.16, 2.0, ("Columbus", "Indianapolis", "Kansas City", "Des Moines", "Omaha")),
    Region("Mid-Atlantic", 0.13, 2.0, ("Philadelphia", "Richmond", "Pittsburgh", "Baltimore")),
    Region("Mountain West", 0.11, 3.0, ("Denver", "Salt Lake City", "Boise", "Albuquerque")),
    Region("Pacific", 0.12, 3.0, ("Sacramento", "Portland", "Spokane", "Fresno")),
    Region("Northeast", 0.07, 3.5, ("Buffalo", "Hartford", "Worcester", "Manchester")),
)

STATE_BY_REGION: dict[str, str] = {
    "Texas & Gulf": "TX",
    "Southeast": "GA",
    "Midwest": "OH",
    "Mid-Atlantic": "PA",
    "Mountain West": "CO",
    "Pacific": "CA",
    "Northeast": "NY",
}
# The warehouse column is still called Province.
PROVINCE_BY_REGION = STATE_BY_REGION


@dataclass(frozen=True)
class StoreFormat:
    """How a store format behaves: order size, cadence and price position."""

    name: str
    is_retail: bool
    weight: float
    # Replenishment orders per month, and lines per order.
    orders_per_month: float
    lines_per_order: float
    # Multiplier on list price. Clubs and online run leaner.
    price_index: float
    # Multiplier on order quantity.
    size_index: float


STORE_FORMATS: tuple[StoreFormat, ...] = (
    StoreFormat("Supercenter", True, 0.38, 5.2, 7.0, 1.00, 3.20),
    StoreFormat("Neighborhood Market", True, 0.22, 6.0, 5.5, 1.01, 1.30),
    StoreFormat("Discount Store", True, 0.14, 4.2, 6.5, 0.99, 1.80),
    StoreFormat("Club Warehouse", True, 0.09, 3.4, 8.0, 0.88, 6.40),
    StoreFormat("Express", True, 0.08, 6.8, 3.2, 1.04, 0.55),
    StoreFormat("Online Fulfillment Center", False, 0.06, 8.5, 8.0, 0.94, 4.10),
    StoreFormat("Pickup & Delivery Hub", False, 0.03, 7.2, 4.5, 0.98, 0.90),
)
CustomerSegment = StoreFormat
CUSTOMER_SEGMENTS = STORE_FORMATS


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
# available to the outlying regions, which makes the late-delivery story a
# network-design problem rather than a carrier problem.
FULFILLMENT_METHODS: tuple[ShippingMethod, ...] = (
    ShippingMethod("DC Ambient", "Private Fleet", 0.31, 0.0, 0.03),
    ShippingMethod("DC Cold Chain", "Private Fleet", 0.19, 0.0, 0.05),
    ShippingMethod("Direct Store Delivery", "Vendor DSD", 0.20, 0.5, 0.07),
    ShippingMethod("Third Party LTL", "Interstate Freight", 0.14, 1.5, 0.24),
    ShippingMethod("Cross-Dock", "Private Fleet", 0.10, 0.0, 0.06),
    ShippingMethod("Vendor Drop-Ship", "Parcel Network", 0.06, 1.0, 0.11),
)
SHIPPING_METHODS = FULFILLMENT_METHODS


# Vendor naming. CPG-flavoured so a vendor scorecard reads like a real one.
SUPPLIER_PREFIXES: tuple[str, ...] = (
    "Crestline", "Harborview", "Kingsford", "Silverbrook", "Ridgemont",
    "Northwind", "Copper Creek", "Stonebridge", "Alderwood", "Blackpine",
    "Sandhill", "Grayrock", "Elkhorn", "Bayfield", "Windrow", "Thornbury",
    "Larkspur", "Fairmont", "Redstone", "Brightwater",
)

SUPPLIER_SUFFIXES: tuple[str, ...] = (
    "Brands", "Consumer Products", "Foods", "Home Goods", "Distribution",
    "Manufacturing", "Supply Co", "Industries", "Global", "Partners",
)


@dataclass
class SalesRep:
    """A market manager. Owns a book of stores across one or more regions."""

    name: str
    # Share of the store book. Deliberately uneven.
    book_share: float
    # Regions this manager covers.
    regions: tuple[str, ...] = field(default_factory=tuple)


# The first manager carries a disproportionate book, which is the concentration
# risk the market-manager dashboard is supposed to surface.
MARKET_MANAGERS: tuple[SalesRep, ...] = (
    SalesRep("Dana Whitfield", 0.26, ("Texas & Gulf", "Southeast")),
    SalesRep("Marcus Oyelaran", 0.16, ("Texas & Gulf",)),
    SalesRep("Priya Raghunathan", 0.14, ("Midwest", "Northeast")),
    SalesRep("Tomasz Bielski", 0.12, ("Mountain West",)),
    SalesRep("Renee Chartrand", 0.11, ("Southeast", "Mid-Atlantic")),
    SalesRep("Sam Okonkwo", 0.09, ("Mid-Atlantic", "Midwest")),
    SalesRep("Hana Sasaki", 0.07, ("Pacific", "Mountain West")),
    SalesRep("Elliot Vance", 0.05, ("Pacific",)),
)
SALES_REPS = MARKET_MANAGERS


ORDER_STATUSES: tuple[str, ...] = ("packed",)

# Store-name vocabulary. Names read as "<locality> <format-ish>", which is how
# store lists actually look once a chain has grown by acquisition.
CUSTOMER_FIRST: tuple[str, ...] = (
    "Northgate", "Cedar Park", "Lakeview", "Riverbend", "Highland", "Fairview",
    "Oakmont", "Summit", "Brookfield", "Stonegate", "Westfield", "Ridgeway",
    "Meadowbrook", "Ironwood", "Clearwater", "Fox Run", "Harvest Point",
    "Silver Lake", "Prairie View", "Kingsport", "Bayside", "Granite Falls",
    "Willow Creek", "Copper Ridge", "Maple Grove", "Redbud", "Sunfield",
    "Trailside", "Union Square", "Vista Park",
)

CUSTOMER_SECOND: tuple[str, ...] = (
    "Supercenter", "Market", "Crossing", "Commons", "Town Center", "Plaza",
    "Marketplace", "Village", "Station", "Landing", "Square", "Junction",
    "Gateway", "Exchange", "Depot", "Pointe", "Galleria", "Center",
)
