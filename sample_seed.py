"""Replace both collections with the nine sample documents.

The target must be a dedicated disposable development database. Set
``ALLOW_DESTRUCTIVE_SEED=true`` explicitly before running this script; the
protected ``attendance_db`` name is always rejected.

The samples show document SHAPES only. The grader uses a much larger hidden dataset with cases they do not show.
Passing on this sample does not mean your analytics are correct - re-read the business rules."""
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def required_mongo_setting(name):
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required configuration: {name}. Set it in the environment or .env.")
    return value


def validate_seed_configuration(mongo_db: str) -> None:
    """Reject unsafe targets before creating a client or touching collections."""
    if mongo_db == "attendance_db":
        raise RuntimeError(
            "Refusing to seed attendance_db. Set MONGO_DB to a dedicated disposable development database."
        )
    if os.getenv("ALLOW_DESTRUCTIVE_SEED", "").strip().lower() != "true":
        raise RuntimeError(
            "Seeding replaces both collections. Set ALLOW_DESTRUCTIVE_SEED=true only for a disposable development database."
        )


def seed() -> None:
    """Validate configuration, then replace the sample collections."""
    mongo_uri = required_mongo_setting("MONGO_URI")
    mongo_db = required_mongo_setting("MONGO_DB")
    validate_seed_configuration(mongo_db)

    here = pathlib.Path(__file__).parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)
    db = MongoClient(mongo_uri, tz_aware=True)[mongo_db]

    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].delete_many({})
        db[name].insert_many(docs)
        print(f"loaded {len(docs):>2} documents into '{db.name}.{name}'")


if __name__ == "__main__":
    seed()
