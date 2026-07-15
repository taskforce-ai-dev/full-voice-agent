import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "rodrigo_kb.db")
LISTINGS_JSON = os.path.join(DATA_DIR, "rodrigo_listings.json")

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSION = 384

DEFAULT_DOCS_DIRECTORY = "knowledge_docs"

os.makedirs(DATA_DIR, exist_ok=True)
