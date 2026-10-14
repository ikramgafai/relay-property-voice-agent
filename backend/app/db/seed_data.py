"""Small demo dataset only. These keys are not an application service taxonomy."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioSeed:
    key: str
    name: str
    description: str
    examples: tuple[str, ...]


def scenario(key: str, description: str, example: str) -> ScenarioSeed:
    return ScenarioSeed(key, key.replace("_", " ").title(), description, (example,))


SERVICE_NAMES = {
    "emergency_maintenance": "Emergency Maintenance",
    "non_emergency_maintenance": "Non-Emergency Maintenance",
    "emergency_non_maintenance": "Emergency Non-Maintenance",
    "leasing": "Leasing",
    "renewal": "Renewal",
    "resident_support": "Resident Support",
    "general_information": "General Information",
    "human_handoff": "Human Handoff",
    "maintenance": "Maintenance",
    "security": "Security",
    "parking": "Parking",
}

ATLAS_SCENARIOS = {
    "emergency_maintenance": (
        scenario(
            "active_flooding",
            "Water actively flooding a resident space.",
            "Water is pouring across my kitchen floor.",
        ),
        scenario(
            "gas_smell", "Reported smell of gas in the building.", "I smell gas near my stove."
        ),
        scenario(
            "electrical_hazard",
            "Reported electrical safety concern.",
            "The leaking water is reaching an electrical outlet.",
        ),
        scenario(
            "burst_pipe",
            "A pipe has burst and is releasing water.",
            "A pipe burst under the bathroom sink.",
        ),
        scenario(
            "severe_water_leak",
            "A substantial ongoing water leak.",
            "Water is streaming through my ceiling.",
        ),
    ),
    "non_emergency_maintenance": (
        scenario(
            "ac_not_cooling",
            "Air conditioning is not cooling adequately.",
            "My AC isn't cooling properly.",
        ),
        scenario(
            "appliance_issue",
            "A household appliance is malfunctioning.",
            "The dishwasher won't finish its cycle.",
        ),
        scenario(
            "minor_plumbing",
            "A minor plumbing issue without an active hazard.",
            "My bathroom faucet drips a little.",
        ),
        scenario(
            "broken_light",
            "A light fixture needs routine attention.",
            "The closet light stopped working.",
        ),
        scenario(
            "cosmetic_issue",
            "A cosmetic repair request.",
            "There is peeling paint on my bedroom wall.",
        ),
    ),
    "emergency_non_maintenance": (
        scenario("break_in", "A reported break-in.", "Someone has broken into my apartment."),
        scenario(
            "immediate_security_threat",
            "A reported immediate security threat.",
            "Someone is threatening me outside my door.",
        ),
        scenario(
            "suspicious_person",
            "A reported suspicious person.",
            "Someone is trying apartment door handles in the hallway.",
        ),
        scenario(
            "resident_safety_issue",
            "A reported resident safety concern.",
            "My neighbor is calling for help in the stairwell.",
        ),
    ),
    "leasing": (
        scenario(
            "availability",
            "Questions about available apartments.",
            "Do you have any one-bedroom apartments available?",
        ),
        scenario(
            "pricing", "Questions about rental pricing.", "What is the monthly rent for a studio?"
        ),
        scenario(
            "schedule_tour",
            "Requests to arrange a property tour.",
            "Can I tour an apartment this Saturday?",
        ),
        scenario(
            "application_question",
            "Questions about the application process.",
            "What documents do I need to apply?",
        ),
    ),
    "renewal": (
        scenario(
            "renewal_eligibility",
            "Questions about renewal eligibility.",
            "Am I eligible to renew my lease?",
        ),
        scenario(
            "renewal_information",
            "Questions about renewal options.",
            "What lease lengths can I choose for renewal?",
        ),
        scenario(
            "renewal_follow_up",
            "Follow-up on a renewal inquiry.",
            "Has anyone reviewed my renewal request?",
        ),
    ),
    "resident_support": (),
    "general_information": (
        scenario(
            "parking",
            "General information about Atlas parking.",
            "Where can my visitors park at Atlas?",
        ),
        scenario(
            "amenities",
            "Questions about community amenities.",
            "What time does the fitness room open?",
        ),
        scenario(
            "office_hours",
            "Questions about office opening hours.",
            "Is the leasing office open on Sunday?",
        ),
        scenario(
            "building_rules",
            "Questions about building rules.",
            "What are the building's quiet hours?",
        ),
    ),
    "human_handoff": (),
}

PALM_SCENARIOS = {
    "maintenance": (
        scenario(
            "cooling_issue",
            "Palm maintenance requests concerning cooling.",
            "My Palm Heights apartment feels hot even with the AC on.",
        ),
        scenario(
            "water_leak",
            "Palm maintenance reports concerning water leaks.",
            "There is water leaking under my sink.",
        ),
    ),
    "security": (
        scenario(
            "security_concern", "Palm security concerns.", "The side entrance lock seems broken."
        ),
    ),
    "parking": (
        scenario(
            "permit_question",
            "Questions about Palm parking permits.",
            "How do I get a parking permit for my new car?",
        ),
        scenario(
            "blocked_space",
            "Reports of an occupied assigned parking space.",
            "Another car is in my assigned space.",
        ),
    ),
    "leasing": (
        scenario(
            "schedule_tour",
            "Palm-specific requests to tour an apartment.",
            "Can I tour a two-bedroom at Palm Heights?",
        ),
    ),
    "general_information": (
        scenario(
            "pool_hours",
            "Questions about Palm's pool hours.",
            "When does the Palm Heights pool close?",
        ),
    ),
    "human_handoff": (
        scenario(
            "speak_to_staff",
            "Requests to speak with Palm staff.",
            "Could I speak to someone in the Palm Heights office?",
        ),
    ),
}
