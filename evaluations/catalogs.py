"""Two demo snapshots and eight synthetic configurations; never production routing data."""

from uuid import NAMESPACE_URL, UUID, uuid5

from app.db.seed_data import ATLAS_SCENARIOS, PALM_SCENARIOS, SERVICE_NAMES
from app.property.schemas import (
    EnabledServiceContext,
    PropertyContext,
    PropertySummary,
    ScenarioContext,
)


def identity(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, "relay-evaluation/" + value)


def custom_service(
    property_key: str,
    key: str,
    name: str,
    description: str,
    scenarios: list[tuple[str, str, str, list[str]]],
) -> EnabledServiceContext:
    return EnabledServiceContext(
        property_service_id=identity(f"{property_key}/{key}"),
        key=key,
        display_name=name,
        description=description,
        priority=0,
        configuration={},
        scenarios=[
            ScenarioContext(
                id=identity(f"{property_key}/{key}/{sc_key}"),
                key=sc_key,
                name=sc_name,
                description=sc_description,
                examples=examples,
            )
            for sc_key, sc_name, sc_description, examples in scenarios
        ],
    )


def catalogs() -> dict[str, PropertyContext]:
    result: dict[str, PropertyContext] = {}
    for slug, name, data in (
        ("atlas", "Atlas Residence", ATLAS_SCENARIOS),
        ("palm", "Palm Heights", PALM_SCENARIOS),
    ):
        result[slug] = PropertyContext(
            property=PropertySummary(id=identity(slug), slug=slug, name=name, timezone="UTC"),
            enabled_services=[
                custom_service(
                    slug,
                    key,
                    SERVICE_NAMES[key],
                    f"Property services for {SERVICE_NAMES[key].lower()}.",
                    [(s.key, s.name, s.description, list(s.examples)) for s in scenarios],
                )
                for key, scenarios in data.items()
            ],
        )
    # Each definition is evaluation-only configuration, including scenario wording differences.
    definitions: dict[str, list[tuple[str, str, str, list[tuple[str, str, str, list[str]]]]]] = {
        "harbor": [
            (
                "package_lockers",
                "Parcel Desk",
                "Locker access and parcel collection.",
                [
                    (
                        "missing_code",
                        "Missing pickup code",
                        "Retrieve an absent parcel code.",
                        ["My package arrived but I have no locker code."],
                    ),
                    (
                        "stuck_door",
                        "Stuck locker door",
                        "A parcel locker will not open.",
                        ["The locker door is stuck with my parcel inside."],
                    ),
                ],
            ),
            (
                "concierge",
                "Harbor Concierge",
                "Visitor reception and local directions.",
                [
                    (
                        "guest_arrival",
                        "Guest arrival",
                        "Help an arriving visitor find reception.",
                        ["My guest needs directions to reception."],
                    ),
                ],
            ),
        ],
        "oakwood": [
            (
                "pet_services",
                "Pet Services",
                "Pet registration, animal policy and pet fees.",
                [
                    (
                        "pet_registration",
                        "Pet registration",
                        "Register a resident's pet.",
                        ["How do I register my dog?"],
                    ),
                    (
                        "pet_policy",
                        "Pet policy",
                        "Questions about permitted household pets.",
                        ["Are cats allowed in the apartments?"],
                    ),
                    (
                        "pet_fees",
                        "Pet fees",
                        "Questions about monthly pet charges.",
                        ["How much is the monthly pet fee?"],
                    ),
                ],
            ),
            (
                "leasing",
                "New Home Team",
                "New apartment applications and tours, excluding renewals.",
                [
                    (
                        "application",
                        "Rental application",
                        "Applying for a new apartment.",
                        ["How can I submit a rental application?"],
                    ),
                ],
            ),
        ],
        "sunset": [
            (
                "amenity_reservations",
                "Amenity Reservations",
                "Reserve shared rooms and courts.",
                [
                    (
                        "court",
                        "Tennis court reservation",
                        "Book time on the tennis court.",
                        ["I would like to reserve the tennis court."],
                    ),
                    (
                        "room",
                        "Community room booking",
                        "Reserve the community room for a gathering.",
                        ["Can I book the community room for a birthday?"],
                    ),
                ],
            ),
            (
                "general_information",
                "Community Guide",
                "Information about amenities, without bookings.",
                [
                    (
                        "hours",
                        "Gym opening hours",
                        "Fitness room opening times.",
                        ["What time does the gym open?"],
                    ),
                ],
            ),
        ],
        "riverstone": [
            (
                "move_in_support",
                "Arrival Team",
                "Moving in, keys and loading access.",
                [
                    (
                        "keys",
                        "Collect keys",
                        "Key collection for a new resident.",
                        ["Where do I collect my move-in keys?"],
                    ),
                    (
                        "loading",
                        "Moving truck access",
                        "Loading bay access for moving day.",
                        ["Where can my moving truck unload?"],
                    ),
                ],
            ),
            (
                "maintenance",
                "Repairs Desk",
                "Repair malfunctioning apartment fixtures.",
                [
                    (
                        "door",
                        "Broken apartment door",
                        "Repair a door that will not close.",
                        ["My apartment door will not close."],
                    ),
                ],
            ),
        ],
        "lofts": [
            (
                "resident_support",
                "Resident Desk",
                "Help with resident account records.",
                [
                    (
                        "account",
                        "Account help",
                        "Help with a resident account.",
                        ["I need account help."],
                    ),
                ],
            ),
            (
                "billing",
                "Billing Desk",
                "Help with resident account charges.",
                [
                    (
                        "charge",
                        "Account help",
                        "Help with a resident account.",
                        ["I need account help."],
                    ),
                    (
                        "invoice",
                        "Invoice copy",
                        "Retrieve a rent invoice.",
                        ["Can I get a copy of my rent invoice?"],
                    ),
                ],
            ),
            ("human_handoff", "Staff Contact", "Requests to speak with a staff member.", []),
        ],
        "cedar": [
            (
                "general_information",
                "Cedar Information",
                "Office hours and building rules only.",
                [
                    (
                        "hours",
                        "Office hours",
                        "When the Cedar office is open.",
                        ["When is the Cedar office open?"],
                    ),
                ],
            ),
        ],
        "metro": [
            (
                "parking",
                "Vehicle Desk",
                "Parking permits, assigned bays and visitor parking.",
                [
                    (
                        "visitor",
                        "Visitor permits",
                        "Visitor vehicle permits.",
                        ["How do I get a visitor parking permit?"],
                    ),
                ],
            ),
            (
                "security",
                "Safety Desk",
                "Building access and security concerns.",
                [
                    (
                        "entry",
                        "Entry concern",
                        "Concerns about building entry security.",
                        ["A stranger is forcing the entrance door."],
                    ),
                ],
            ),
            (
                "package_lockers",
                "Parcel Kiosk",
                "Parcel retrieval at the Metro kiosk.",
                [
                    (
                        "scan",
                        "Barcode trouble",
                        "The parcel kiosk cannot read a barcode.",
                        ["The parcel kiosk will not scan my barcode."],
                    ),
                ],
            ),
        ],
        "empty": [],
    }
    for slug, services in definitions.items():
        result[slug] = PropertyContext(
            property=PropertySummary(
                id=identity(slug), slug=slug, name=slug.title(), timezone="UTC"
            ),
            enabled_services=[custom_service(slug, *service) for service in services],
        )
    return result
