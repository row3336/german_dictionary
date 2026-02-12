from pathlib import Path

from bot.storage import init_db, migrate_json_to_sqlite


def main():
    json_path = Path("./data/words.json")
    db_path = Path("./data/words.db")

    init_db(db_path)
    inserted = migrate_json_to_sqlite(json_path, db_path)
    print(f"Migrated records: {inserted}")
    print(f"JSON source: {json_path.resolve()}")
    print(f"SQLite target: {db_path.resolve()}")


if __name__ == "__main__":
    main()
