import re


VALID_VEHICLE_NUMBER_LENGTHS = {8, 9, 10}


def normalize_vehicle_number(vehicle_number):
    """Keep only alphanumeric characters for vehicle-number processing."""
    if vehicle_number is None:
        return ""

    text = str(vehicle_number).strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""

    return re.sub(r"[^A-Za-z0-9]", "", text)


def is_vehicle_number_eligible(vehicle_number):
    """Eligible numbers are alphanumeric-only values with length 8, 9, or 10."""
    normalized = normalize_vehicle_number(vehicle_number)
    return len(normalized) in VALID_VEHICLE_NUMBER_LENGTHS
