"""Sanity-check the office coordinates the watcher is filtering against.

OpenStreetMap has no rooftop entry for 402 Dunsmuir St, so the default in
config.py is a street-level estimate. Since a 700 m radius is sensitive to being
a block or two off, confirm it: open the Google Maps link this prints, and if the
pin isn't on the building, right-click the building > copy coordinates and set
OFFICE_LAT / OFFICE_LON in .env.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
import craigslist  # noqa: E402

LAT, LON = config.OFFICE_LAT, config.OFFICE_LON

print(f"Office:  {LAT}, {LON}")
print(f"Radius:  {config.RADIUS_M:.0f} m straight-line "
      f"(~{craigslist.walk_minutes(config.RADIUS_M)} min walk)")
print(f"Map:     https://www.google.com/maps/search/?api=1&query={LAT},{LON}")
print()

listings = craigslist.search()
print(f"{len(listings)} listings currently match.")
if listings:
    nearest = min(listings, key=lambda i: i["distance_m"])
    farthest = max(listings, key=lambda i: i["distance_m"])
    print(f"  nearest:  {nearest['distance_m']:>4} m  {nearest['title'][:50]}")
    print(f"  farthest: {farthest['distance_m']:>4} m  {farthest['title'][:50]}")
