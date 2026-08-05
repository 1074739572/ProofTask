"""Product catalog with a stub search function.

The eval prompt is intentionally VAGUE ("add a search feature"). The exact
search contract is documented in README.md — a good agent reads the repo
instead of guessing. The stub below returns everything, which is wrong.
"""

PRODUCTS = [
    {"id": 1, "name": "Wireless Mouse", "category": "Accessories"},
    {"id": 2, "name": "Mechanical Keyboard", "category": "Accessories"},
    {"id": 3, "name": "USB-C Hub", "category": "Accessories"},
    {"id": 4, "name": "27-inch Monitor", "category": "Displays"},
    {"id": 5, "name": "Laptop Stand", "category": "Ergonomics"},
    {"id": 6, "name": "USB Cable", "category": "Accessories"},
    {"id": 7, "name": "Webcam", "category": "Accessories"},
    {"id": 8, "name": "Desk Lamp", "category": "Ergonomics"},
    {"id": 9, "name": "HDMI Cable", "category": "Accessories"},
    {"id": 10, "name": "Monitor Arm", "category": "Ergonomics"},
    {"id": 11, "name": "Keyboard Wrist Rest", "category": "Ergonomics"},
    {"id": 12, "name": "Docking Station", "category": "Accessories"},
]


def search_products(query):
    """Search products. See README.md for the exact contract."""
    # BUG: stub returns everything regardless of query.
    return list(PRODUCTS)
