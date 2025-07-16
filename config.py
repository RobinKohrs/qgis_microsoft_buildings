import os

# User must set this to the path of the manifest CSV file
USER_MANIFEST_PATH = os.path.join(os.path.expanduser("~"), ".ms_buildings_roads", "dataset-links.csv")

# Optionally, allow override via environment variable
MANIFEST_PATH = os.environ.get("MS_BUILDINGS_MANIFEST", USER_MANIFEST_PATH)

def get_manifest_path():
    if not os.path.exists(MANIFEST_PATH):
        raise FileNotFoundError(f"Manifest CSV not found at {MANIFEST_PATH}. Please download it manually and set the path in config.py or via the MS_BUILDINGS_MANIFEST environment variable.")
    return MANIFEST_PATH 